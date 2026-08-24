from __future__ import annotations

from typing import Any

from fwrouter_api.services import rules as rules_service
from fwrouter_api.services.rules_state_files import get_manual_rules_texts
from fwrouter_api.services.rules_state_store import (
    _json_dumps,
    _json_loads,
    _upsert_rules_state_record,
    get_rules_state,
)


def list_rules_metadata() -> list[dict[str, Any]]:
    with rules_service.db_session() as connection:
        rows = connection.execute(
            """
            SELECT
                ruleset_id,
                ruleset_type,
                version_name,
                source_url,
                active_path,
                downloaded_at,
                activated_at,
                status,
                last_success_at,
                last_failed_at,
                last_error_code,
                last_error_message,
                last_job_id,
                metadata_json
            FROM rules_metadata
            ORDER BY ruleset_type, ruleset_id
            """
        ).fetchall()

    return [
        {
            "ruleset_id": row["ruleset_id"],
            "ruleset_type": row["ruleset_type"],
            "version_name": row["version_name"],
            "source_url": row["source_url"],
            "active_path": row["active_path"],
            "downloaded_at": row["downloaded_at"],
            "activated_at": row["activated_at"],
            "status": row["status"],
            "last_success_at": row["last_success_at"],
            "last_failed_at": row["last_failed_at"],
            "last_error_code": row["last_error_code"],
            "last_error_message": row["last_error_message"],
            "last_job_id": row["last_job_id"],
            "metadata_json": _json_loads(row["metadata_json"]),
        }
        for row in rows
    ]

def _upsert_ruleset_metadata(
    *,
    ruleset_type: str,
    active_path: str,
    status: str,
    job_id: str,
    metadata: dict[str, Any],
    version_name: str | None = None,
    source_urls: list[str] | None = None,
    error_code: str | None = None,
    error_message: str | None = None,
) -> None:
    now = rules_service._utc_now_iso()
    source_url = ",".join(source_urls or [])
    with rules_service.db_session() as connection:
        connection.execute(
            """
            INSERT INTO rules_metadata (
                ruleset_id,
                ruleset_type,
                version_name,
                source_url,
                active_path,
                downloaded_at,
                activated_at,
                status,
                last_success_at,
                last_failed_at,
                last_error_code,
                last_error_message,
                last_job_id,
                metadata_json
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(ruleset_id) DO UPDATE SET
                ruleset_type = excluded.ruleset_type,
                version_name = excluded.version_name,
                source_url = excluded.source_url,
                active_path = excluded.active_path,
                downloaded_at = excluded.downloaded_at,
                activated_at = excluded.activated_at,
                status = excluded.status,
                last_success_at = excluded.last_success_at,
                last_failed_at = excluded.last_failed_at,
                last_error_code = excluded.last_error_code,
                last_error_message = excluded.last_error_message,
                last_job_id = excluded.last_job_id,
                metadata_json = excluded.metadata_json
            """,
            (
                ruleset_type,
                ruleset_type,
                version_name,
                source_url,
                active_path,
                now,
                now if status in {"active", "success"} else None,
                status,
                now if status in {"active", "success"} else None,
                now if status == "failed" else None,
                error_code,
                error_message,
                job_id,
                _json_dumps(metadata),
            ),
        )


def update_rules_metadata_records(
    *,
    job_id: str,
    effective_artifact: dict[str, Any],
    big_direct_version: str | None = None,
    big_vpn_version: str | None = None,
    source_urls: dict[str, list[str]] | None = None,
    fetch_summary: dict[str, Any] | None = None,
    status: str = "active",
    error_code: str | None = None,
    error_message: str | None = None,
) -> None:
    paths = get_manual_rules_texts()
    counts = effective_artifact.get("source_counts", {})
    effective_counts = effective_artifact.get("effective_counts", {})
    urls = source_urls or {}

    rows = [
        (rules_service.RULESET_MANUAL, str(paths["active_path"]), None, {"count": counts.get(rules_service.RULESET_MANUAL, 0)}),
        (rules_service.RULESET_STATIC_DIRECT, str(paths["static_direct_path"]), None, {"count": counts.get(rules_service.RULESET_STATIC_DIRECT, 0)}),
        (
            rules_service.RULESET_BIG_DIRECT,
            str(paths["big_direct_path"]),
            big_direct_version,
            {"count": counts.get(rules_service.RULESET_BIG_DIRECT, 0), "fetch_summary": (fetch_summary or {}).get(rules_service.RULESET_BIG_DIRECT, {})},
        ),
        (
            rules_service.RULESET_BIG_VPN,
            str(paths["big_vpn_path"]),
            big_vpn_version,
            {
                "count": counts.get(rules_service.RULESET_BIG_VPN, 0),
                "fetch_summary": (fetch_summary or {}).get(rules_service.RULESET_BIG_VPN, {}),
                "rules_pipeline_version": rules_service.RULES_PIPELINE_VERSION,
            },
        ),
        (
            rules_service.RULESET_EFFECTIVE,
            str(paths["effective_json_path"]),
            None,
            {"source_counts": counts, "effective_counts": effective_counts, "selective_default": effective_artifact.get("selective_default"), "fetch_summary": fetch_summary or {}},
        ),
    ]

    for ruleset_type, active_path, version_name, metadata in rows:
        _upsert_ruleset_metadata(
            ruleset_type=ruleset_type,
            active_path=active_path,
            status=status,
            job_id=job_id,
            metadata=metadata,
            version_name=version_name,
            source_urls=urls.get(ruleset_type, []),
            error_code=error_code,
            error_message=error_message,
        )


def mark_rules_metadata_update_failed(
    *,
    job_id: str,
    code: str,
    message: str,
) -> None:
    """Record a failed update without replacing last known active metadata."""
    now = rules_service._utc_now_iso()
    paths = get_manual_rules_texts()
    rows = [
        (rules_service.RULESET_MANUAL, str(paths["active_path"]), ""),
        (rules_service.RULESET_STATIC_DIRECT, str(paths["static_direct_path"]), ""),
        (
            rules_service.RULESET_BIG_DIRECT,
            str(paths["big_direct_path"]),
            ",".join(rules_service._configured_rules_sources().get(rules_service.RULESET_BIG_DIRECT, [])),
        ),
        (
            rules_service.RULESET_BIG_VPN,
            str(paths["big_vpn_path"]),
            ",".join(rules_service._configured_rules_sources().get(rules_service.RULESET_BIG_VPN, [])),
        ),
        (rules_service.RULESET_EFFECTIVE, str(paths["effective_json_path"]), ""),
    ]

    with rules_service.db_session() as connection:
        for ruleset_type, active_path, source_url in rows:
            existing = connection.execute(
                "SELECT status FROM rules_metadata WHERE ruleset_id = ?",
                (ruleset_type,),
            ).fetchone()
            if existing is None:
                connection.execute(
                    """
                    INSERT INTO rules_metadata (
                        ruleset_id,
                        ruleset_type,
                        source_url,
                        active_path,
                        downloaded_at,
                        activated_at,
                        status,
                        last_failed_at,
                        last_error_code,
                        last_error_message,
                        last_job_id,
                        metadata_json
                    )
                    VALUES (?, ?, ?, ?, ?, NULL, 'failed', ?, ?, ?, ?, ?)
                    """,
                    (
                        ruleset_type,
                        ruleset_type,
                        source_url,
                        active_path,
                        now,
                        now,
                        code,
                        message,
                        job_id,
                        _json_dumps({"count": 0}),
                    ),
                )
                continue

            preserved_status = str(existing["status"] or "")
            next_status = "failed" if preserved_status in {"", "not_configured", "running"} else preserved_status
            connection.execute(
                """
                UPDATE rules_metadata
                SET
                    source_url = CASE WHEN ? != '' THEN ? ELSE source_url END,
                    active_path = ?,
                    status = ?,
                    last_failed_at = ?,
                    last_error_code = ?,
                    last_error_message = ?,
                    last_job_id = ?
                WHERE ruleset_id = ?
                """,
                (
                    source_url,
                    source_url,
                    active_path,
                    next_status,
                    now,
                    code,
                    message,
                    job_id,
                    ruleset_type,
                ),
            )


def mark_rules_job_running(*, job_id: str, update_type: str) -> dict[str, Any]:
    state = get_rules_state()
    return _upsert_rules_state_record(
        {
            **state,
            "status": "running",
            "last_apply_job_id": job_id if update_type == "manual_apply" else state["last_apply_job_id"],
            "last_update_job_id": job_id if update_type == "full_update" else state["last_update_job_id"],
            "error_code": None,
            "error_message": None,
        }
    )


def _repair_stale_running_rules_state(state: dict[str, Any]) -> dict[str, Any]:
    if state.get("status") != "running":
        return state

    from fwrouter_api.services import jobs as jobs_service

    active_job = jobs_service.find_active_lock_conflict(rules_service.LOCK_RULES_APPLY)
    if active_job is not None:
        return state

    now = rules_service._utc_now_iso()
    return _upsert_rules_state_record(
        {
            **state,
            "status": "failed",
            "last_failed_at": now,
            "error_code": "RULES_JOB_STALE",
            "error_message": "Rules state was running, but no active rules job exists.",
        }
    )


def mark_rules_job_failed(
    *,
    job_id: str,
    code: str,
    message: str,
    update_type: str,
    effective_artifact: dict[str, Any] | None = None,
    source_urls: dict[str, list[str]] | None = None,
    fetch_summary: dict[str, Any] | None = None,
) -> dict[str, Any]:
    del update_type
    now = rules_service._utc_now_iso()
    state = get_rules_state()
    updated = _upsert_rules_state_record(
        {**state, "status": "failed", "last_failed_at": now, "error_code": code, "error_message": message}
    )
    del effective_artifact, source_urls, fetch_summary
    mark_rules_metadata_update_failed(job_id=job_id, code=code, message=message)
    return updated


def mark_rules_job_success(
    *,
    job_id: str,
    update_type: str,
) -> dict[str, Any]:
    now = rules_service._utc_now_iso()
    state = get_rules_state()
    updated = _upsert_rules_state_record(
        {
            **state,
            "status": "success",
            "last_apply_job_id": job_id if update_type == "manual_apply" else state["last_apply_job_id"],
            "last_update_job_id": job_id if update_type == "full_update" else state["last_update_job_id"],
            "last_success_at": now,
            "error_code": None,
            "error_message": None,
        }
    )
    return updated
