from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
import sqlite3
from pathlib import Path
from typing import Any

from fwrouter_api.db.connection import db_session
from fwrouter_api.db.connection import get_db_path
from fwrouter_api.services.apply_versions_retention import cleanup_apply_versions_retention
from fwrouter_api.services.jobs import compact_oversized_job_results
from fwrouter_api.services.jobs_retention import cleanup_jobs_retention
from fwrouter_api.services.logs_retention import cleanup_log_retention
from fwrouter_api.services.logs import write_operational_log
from fwrouter_api.services.servers import expire_global_fixed_server
from fwrouter_api.services.state_retention import cleanup_state_retention
from fwrouter_api.services.subject_policy import expire_subject_overrides
from fwrouter_api.services.traffic import cleanup_traffic_history


OPERATIONAL_LOG_RETENTION_DAYS = 3
TECHNICAL_LOG_RETENTION_DAYS = 14
SUBJECT_RETENTION_DAYS = 90
SERVER_PREFERENCE_RETENTION_DAYS = 90


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _sqlite_cutoff(days: int) -> str:
    return (_utc_now() - timedelta(days=days)).replace(microsecond=0).strftime("%Y-%m-%d %H:%M:%S")


def _collect_database_storage_stats() -> dict[str, Any]:
    db_path = get_db_path()
    connection = sqlite3.connect(db_path)
    try:
        page_count = int(connection.execute("PRAGMA page_count").fetchone()[0])
        page_size = int(connection.execute("PRAGMA page_size").fetchone()[0])
        freelist_count = int(connection.execute("PRAGMA freelist_count").fetchone()[0])
    finally:
        connection.close()

    allocated_bytes = page_count * page_size
    free_bytes = freelist_count * page_size
    return {
        "db_path": str(db_path),
        "page_count": page_count,
        "page_size": page_size,
        "freelist_count": freelist_count,
        "allocated_bytes": allocated_bytes,
        "free_bytes": free_bytes,
        "file_size_bytes": db_path.stat().st_size if db_path.exists() else 0,
    }


def _path_size(path: Path) -> int:
    try:
        if path.is_file():
            return path.stat().st_size
        if path.is_dir():
            total = 0
            for child in path.rglob("*"):
                if child.is_file():
                    total += child.stat().st_size
            return total
    except OSError:
        return 0
    return 0


def _collect_filesystem_storage_stats() -> dict[str, Any]:
    from fwrouter_api.core.config import get_settings

    settings = get_settings()
    state_dir = settings.paths.state_dir
    jobs_dir = settings.paths.jobs_dir
    generated_dataplane = settings.paths.generated_dir / "dataplane"
    last_good_dataplane = state_dir / "last-good" / "dataplane"
    buckets = {
        "state_root": state_dir,
        "jobs": jobs_dir,
        "generated_dataplane": generated_dataplane,
        "generated_dataplane_profiles": generated_dataplane / "profiles",
        "last_good_dataplane": last_good_dataplane,
        "last_good_dataplane_snapshots": last_good_dataplane / "snapshots",
    }
    return {
        name: {
            "path": str(path),
            "exists": path.exists(),
            "size_bytes": _path_size(path) if path.exists() else 0,
        }
        for name, path in buckets.items()
    }


def _sum_report_bytes(value: Any, keys: tuple[str, ...]) -> int:
    if not isinstance(value, dict):
        return 0
    total = 0
    for key in keys:
        raw = value.get(key)
        if isinstance(raw, int):
            total += raw
    return total


def _collect_reclaimable_storage_estimate(
    *,
    jobs_retention: dict[str, Any],
    apply_versions_retention: dict[str, Any],
    state_retention: dict[str, Any],
) -> dict[str, Any]:
    jobs_bytes = _sum_report_bytes(
        jobs_retention,
        ("candidates_artifact_size_bytes", "orphan_artifact_dirs_size_bytes"),
    )
    generated_dataplane_bytes = _sum_report_bytes(
        apply_versions_retention,
        ("candidates_size_bytes", "orphan_manifest_candidates_size_bytes"),
    )
    dataplane_snapshots = state_retention.get("dataplane_snapshots")
    snapshot_bytes = _sum_report_bytes(
        dataplane_snapshots,
        ("candidates_size_bytes",),
    )
    return {
        "jobs_bytes": jobs_bytes,
        "generated_dataplane_bytes": generated_dataplane_bytes,
        "last_good_dataplane_snapshots_bytes": snapshot_bytes,
        "total_bytes": jobs_bytes + generated_dataplane_bytes + snapshot_bytes,
    }


def cleanup_xray_legacy_subscription_shadows(*, dry_run: bool) -> dict[str, Any]:
    with db_session() as connection:
        candidates = [
            dict(row)
            for row in connection.execute(
                """
                SELECT
                    s.subject_id,
                    s.display_name,
                    s.alias,
                    s.is_active,
                    s.last_seen_at,
                    s.last_traffic_at,
                    sx.client_id,
                    sx.client_uuid,
                    sx.email,
                    sc.token AS subscription_token,
                    sc.display_name AS subscription_display_name,
                    sc.last_seen_at AS subscription_last_seen_at
                FROM subjects AS s
                JOIN subject_xray AS sx ON sx.subject_id = s.subject_id
                JOIN subscription_clients AS sc
                  ON lower(sc.token) = lower(substr(sx.email, 1, instr(sx.email, '@') - 1))
                WHERE s.is_deleted = 0
                  AND s.subject_type = 'xray'
                  AND s.is_active = 0
                  AND COALESCE(s.last_traffic_at, '') = ''
                  AND sx.email NOT LIKE 'sub-%'
                  AND sx.email NOT LIKE 'vpn-auto-%'
                ORDER BY lower(sc.token), s.subject_id
                """
            ).fetchall()
        ]

        soft_deleted_count = 0
        if candidates and not dry_run:
            subject_ids = [str(row["subject_id"]) for row in candidates]
            placeholders = ", ".join("?" for _ in subject_ids)
            soft_deleted_count = connection.execute(
                f"""
                UPDATE subjects
                SET
                    is_deleted = 1,
                    is_active = 0,
                    deleted_at = CURRENT_TIMESTAMP,
                    updated_at = CURRENT_TIMESTAMP,
                    metadata_json = json_set(
                        COALESCE(metadata_json, json('{{}}')),
                        '$.cleanup',
                        json(?)
                    )
                WHERE subject_id IN ({placeholders})
                """,
                (
                    json.dumps(
                        {
                            "source": "maintenance",
                            "reason": "xray_legacy_subscription_shadow",
                        },
                        ensure_ascii=False,
                        sort_keys=True,
                    ),
                    *subject_ids,
                ),
            ).rowcount

    return {
        "dry_run": dry_run,
        "candidates_count": len(candidates),
        "candidates": candidates,
        "soft_deleted_count": soft_deleted_count,
    }


def _maintain_database_storage(*, dry_run: bool, vacuum_requested: bool) -> dict[str, Any]:
    before = _collect_database_storage_stats()
    result = {
        "dry_run": dry_run,
        "vacuum_requested": vacuum_requested,
        "vacuumed": False,
        "optimize_ran": False,
        "before": before,
        "after": before,
    }
    if dry_run:
        result["vacuum_recommended"] = vacuum_requested or before["free_bytes"] > 8 * 1024 * 1024
        return result

    connection = sqlite3.connect(get_db_path())
    try:
        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        if vacuum_requested:
            connection.execute("VACUUM")
            result["vacuumed"] = True
        connection.execute("PRAGMA optimize")
        result["optimize_ran"] = True
    finally:
        connection.close()

    result["after"] = _collect_database_storage_stats()
    return result


def run_control_plane_maintenance(*, dry_run: bool = True) -> dict[str, Any]:
    operational_logs_cutoff = _sqlite_cutoff(OPERATIONAL_LOG_RETENTION_DAYS)
    subjects_cutoff = _sqlite_cutoff(SUBJECT_RETENTION_DAYS)
    server_preferences_cutoff = _sqlite_cutoff(SERVER_PREFERENCE_RETENTION_DAYS)

    storage_before = _collect_filesystem_storage_stats()
    override_expiry = expire_subject_overrides(dry_run=dry_run)
    global_fixed_server_expiry = expire_global_fixed_server(
        dry_run=dry_run,
        apply_runtime=not dry_run,
    )

    with db_session() as connection:
        operational_logs = [
            dict(row)
            for row in connection.execute(
                """
                SELECT event_id, event_type, created_at
                FROM operational_logs
                WHERE created_at < ?
                ORDER BY created_at
                """,
                (operational_logs_cutoff,),
            ).fetchall()
        ]
        stale_subjects = [
            dict(row)
            for row in connection.execute(
                """
                SELECT subject_id, subject_type, is_active, is_deleted, last_seen_at, deleted_at
                FROM subjects
                WHERE is_active = 0
                  AND (
                    COALESCE(deleted_at, last_seen_at, created_at) < ?
                  )
                ORDER BY COALESCE(deleted_at, last_seen_at, created_at)
                """,
                (subjects_cutoff,),
            ).fetchall()
        ]
        stale_server_preferences = [
            dict(row)
            for row in connection.execute(
                """
                SELECT p.server_id, s.inventory_state, s.missing_since, p.remembered_until
                FROM server_preferences p
                JOIN servers s ON s.server_id = p.server_id
                WHERE s.inventory_state != 'active'
                  AND COALESCE(s.missing_since, s.updated_at) < ?
                ORDER BY COALESCE(s.missing_since, s.updated_at)
                """,
                (server_preferences_cutoff,),
            ).fetchall()
        ]

        deleted_operational_logs_count = 0
        deleted_subjects_count = 0
        deleted_server_preferences_count = 0

        if not dry_run:
            if operational_logs:
                deleted_operational_logs_count = connection.execute(
                    """
                    DELETE FROM operational_logs
                    WHERE created_at < ?
                    """,
                    (operational_logs_cutoff,),
                ).rowcount
            if stale_subjects:
                deleted_subjects_count = connection.execute(
                    """
                    DELETE FROM subjects
                    WHERE is_active = 0
                      AND (
                        COALESCE(deleted_at, last_seen_at, created_at) < ?
                      )
                    """,
                    (subjects_cutoff,),
                ).rowcount
            if stale_server_preferences:
                deleted_server_preferences_count = connection.execute(
                    """
                    DELETE FROM server_preferences
                    WHERE server_id IN (
                        SELECT p.server_id
                        FROM server_preferences p
                        JOIN servers s ON s.server_id = p.server_id
                        WHERE s.inventory_state != 'active'
                          AND COALESCE(s.missing_since, s.updated_at) < ?
                    )
                    """,
                    (server_preferences_cutoff,),
                ).rowcount

    jobs_retention = cleanup_jobs_retention(dry_run=dry_run)
    job_results_compaction = compact_oversized_job_results(dry_run=dry_run)
    apply_versions_retention = cleanup_apply_versions_retention(dry_run=dry_run)
    log_retention = cleanup_log_retention(
        operational_retention_days=OPERATIONAL_LOG_RETENTION_DAYS,
        technical_retention_days=TECHNICAL_LOG_RETENTION_DAYS,
        dry_run=dry_run,
    )
    state_retention = cleanup_state_retention(dry_run=dry_run)
    traffic_history = cleanup_traffic_history(dry_run=dry_run)
    xray_legacy_shadows = cleanup_xray_legacy_subscription_shadows(dry_run=dry_run)
    database_storage = _maintain_database_storage(
        dry_run=dry_run,
        vacuum_requested=any(
            (
                jobs_retention["deleted_jobs_count"] > 0,
                job_results_compaction["updated_jobs_count"] > 0,
                apply_versions_retention["deleted_apply_versions_count"] > 0,
                xray_legacy_shadows["soft_deleted_count"] > 0,
            )
        ),
    )
    storage_after = _collect_filesystem_storage_stats()
    storage_reclaimable_estimate = _collect_reclaimable_storage_estimate(
        jobs_retention=jobs_retention,
        apply_versions_retention=apply_versions_retention,
        state_retention=state_retention,
    )
    result = {
        "dry_run": dry_run,
        "storage": {
            "before": storage_before,
            "after": storage_after,
            "reclaimable_estimate": storage_reclaimable_estimate,
        },
        "override_expiry": override_expiry,
        "global_fixed_server_expiry": global_fixed_server_expiry,
        "operational_logs": {
            "retention_days": OPERATIONAL_LOG_RETENTION_DAYS,
            "cutoff": operational_logs_cutoff,
            "candidates_count": len(operational_logs),
            "candidates": operational_logs,
            "deleted_count": deleted_operational_logs_count,
        },
        "subjects": {
            "retention_days": SUBJECT_RETENTION_DAYS,
            "cutoff": subjects_cutoff,
            "candidates_count": len(stale_subjects),
            "candidates": stale_subjects,
            "deleted_count": deleted_subjects_count,
        },
        "server_preferences": {
            "retention_days": SERVER_PREFERENCE_RETENTION_DAYS,
            "cutoff": server_preferences_cutoff,
            "candidates_count": len(stale_server_preferences),
            "candidates": stale_server_preferences,
            "deleted_count": deleted_server_preferences_count,
        },
        "traffic_history": traffic_history,
        "jobs_retention": jobs_retention,
        "job_results_compaction": job_results_compaction,
        "apply_versions_retention": apply_versions_retention,
        "log_retention": log_retention,
        "state_retention": state_retention,
        "xray_legacy_shadows": xray_legacy_shadows,
        "database_storage": database_storage,
    }

    if not dry_run:
        write_operational_log(
            event_type="control_plane_maintenance_completed",
            message="Control-plane maintenance completed.",
            details={
                "override_expiry": {
                    "expired_user_overrides_count": override_expiry["expired_user_overrides_count"],
                    "expired_server_overrides_count": override_expiry["expired_server_overrides_count"],
                },
                "expired_global_fixed_server_count": global_fixed_server_expiry[
                    "expired_global_fixed_server_count"
                ],
                "operational_logs_deleted_count": deleted_operational_logs_count,
                "subjects_deleted_count": deleted_subjects_count,
                "server_preferences_deleted_count": deleted_server_preferences_count,
                "traffic_history_candidates_count": traffic_history["candidates_count"],
                "traffic_history_deleted_count": traffic_history["deleted_count"],
                "traffic_invalid_snapshot_candidates_count": traffic_history["invalid_snapshot_candidates_count"],
                "traffic_invalid_snapshots_deleted_count": traffic_history["deleted_invalid_snapshots_count"],
                "jobs_retention_candidates_count": jobs_retention["candidates_count"],
                "jobs_artifact_dirs_deleted_count": jobs_retention["deleted_artifact_dirs_count"],
                "jobs_artifact_bytes_deleted": jobs_retention.get("deleted_artifact_bytes", 0),
                "job_results_compacted_count": job_results_compaction["updated_jobs_count"],
                "job_results_bytes_saved": job_results_compaction["bytes_saved"],
                "apply_versions_deleted_count": apply_versions_retention["deleted_apply_versions_count"],
                "apply_version_manifest_files_deleted_count": apply_versions_retention["deleted_manifest_files_count"],
                "apply_version_manifest_bytes_deleted": apply_versions_retention.get("deleted_manifest_bytes", 0),
                "technical_log_lines_deleted_count": log_retention["technical"]["deleted_lines_count"],
                "operational_jsonl_deleted_lines_count": log_retention["operational"]["deleted_lines"],
                "dataplane_snapshots_deleted_count": state_retention["dataplane_snapshots"]["deleted_count"],
                "dataplane_snapshot_bytes_deleted": state_retention["dataplane_snapshots"].get("deleted_bytes", 0),
                "debug_artifacts_deleted_count": state_retention["debug_artifacts"]["deleted_count"],
                "xray_legacy_shadow_candidates_count": xray_legacy_shadows["candidates_count"],
                "xray_legacy_shadow_soft_deleted_count": xray_legacy_shadows["soft_deleted_count"],
                "database_vacuumed": database_storage["vacuumed"],
            },
            dedupe_key=operational_logs_cutoff,
            cooldown_seconds=3600,
        )

    return result
