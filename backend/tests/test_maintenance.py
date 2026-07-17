from __future__ import annotations

import json
import os
from pathlib import Path

from fwrouter_api.core.config import get_settings
from fwrouter_api.db.connection import db_session, initialize_database
from fwrouter_api.services.jobs import create_job, mark_job_running, mark_job_success
from fwrouter_api.services.maintenance import run_control_plane_maintenance


def _configure_env(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("FWROUTER_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("FWROUTER_DATABASE_URL", f"sqlite:///{tmp_path}/fwrouter.db")
    monkeypatch.setenv("FWROUTER_JOB_RESULT_MAX_BYTES", "4096")
    get_settings.cache_clear()


def test_control_plane_maintenance_compacts_jobs_and_cleans_apply_versions(
    monkeypatch,
    tmp_path: Path,
) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()

    job = create_job("noop", requested_by="pytest")
    mark_job_running(job["job_id"])
    mark_job_success(job["job_id"], result={"job_status": "success", "blob": "x" * 1000})

    settings = get_settings()
    dataplane_dir = settings.paths.generated_dir / "dataplane"
    dataplane_dir.mkdir(parents=True, exist_ok=True)
    old_manifest_path = dataplane_dir / "11111111-1111-1111-1111-111111111111.json"
    recent_manifest_path = dataplane_dir / "22222222-2222-2222-2222-222222222222.json"
    orphan_manifest_path = dataplane_dir / "33333333-3333-3333-3333-333333333333.json"
    current_manifest_path = dataplane_dir / "current-manifest.json"
    debug_dir = settings.paths.state_dir / "debug" / "core-check-old"
    debug_dir.mkdir(parents=True, exist_ok=True)

    manifest_payload = {"plan_id": "fixture", "summary": {"global_mode": "direct"}}
    old_manifest_path.write_text(json.dumps(manifest_payload), encoding="utf-8")
    recent_manifest_path.write_text(json.dumps(manifest_payload), encoding="utf-8")
    orphan_manifest_path.write_text(json.dumps(manifest_payload), encoding="utf-8")
    current_manifest_path.write_text(json.dumps(manifest_payload), encoding="utf-8")
    (debug_dir / "note.txt").write_text("debug", encoding="utf-8")

    with db_session() as connection:
        connection.execute(
            "UPDATE jobs SET result_json = ? WHERE job_id = ?",
            (json.dumps({"job_status": "success", "stage": "legacy", "blob": "y" * 20000}), job["job_id"]),
        )
        connection.execute(
            """
            INSERT INTO apply_versions (
                apply_id, job_id, manifest_path, created_at, promoted_at, status, summary_json
            ) VALUES (?, ?, ?, datetime('now', '-10 days'), NULL, ?, ?)
            """,
            (
                "11111111-1111-1111-1111-111111111111",
                job["job_id"],
                str(old_manifest_path),
                "generated",
                json.dumps({"state": "old"}),
            ),
        )
        connection.execute(
            """
            INSERT INTO apply_versions (
                apply_id, job_id, manifest_path, created_at, promoted_at, status, summary_json
            ) VALUES (?, ?, ?, datetime('now'), NULL, ?, ?)
            """,
            (
                "22222222-2222-2222-2222-222222222222",
                job["job_id"],
                str(recent_manifest_path),
                "generated",
                json.dumps({"state": "recent"}),
            ),
        )
        for index in range(19):
            apply_id = f"99999999-9999-9999-9999-{index:012d}"
            connection.execute(
                """
                INSERT INTO apply_versions (
                    apply_id, job_id, manifest_path, created_at, promoted_at, status, summary_json
                ) VALUES (?, ?, ?, datetime('now', '-1 minutes'), NULL, ?, ?)
                """,
                (
                    apply_id,
                    job["job_id"],
                    str(dataplane_dir / f"{apply_id}.json"),
                    "generated",
                    json.dumps({"state": "recent-extra"}),
                ),
            )

    old_timestamp = (settings.paths.state_dir / "fwrouter.db").stat().st_mtime
    old_manifest_mtime = old_timestamp - 20 * 86400
    for path in (old_manifest_path, orphan_manifest_path, debug_dir):
        path.touch()
    for path in (old_manifest_path, orphan_manifest_path, debug_dir, debug_dir / "note.txt"):
        if path.exists():
            path.chmod(0o644) if path.is_file() else None
    os.utime(old_manifest_path, (old_manifest_mtime, old_manifest_mtime))
    os.utime(orphan_manifest_path, (old_manifest_mtime, old_manifest_mtime))
    os.utime(debug_dir, (old_manifest_mtime, old_manifest_mtime))
    os.utime(debug_dir / "note.txt", (old_manifest_mtime, old_manifest_mtime))

    result = run_control_plane_maintenance(dry_run=False)

    assert result["job_results_compaction"]["updated_jobs_count"] == 1
    assert result["apply_versions_retention"]["deleted_apply_versions_count"] == 1
    assert result["apply_versions_retention"]["orphan_manifest_deleted_count"] == 1
    assert result["state_retention"]["debug_artifacts"]["deleted_count"] == 1
    assert result["database_storage"]["vacuumed"] is True

    assert not old_manifest_path.exists()
    assert recent_manifest_path.exists()
    assert not orphan_manifest_path.exists()
    assert current_manifest_path.exists()
    assert not debug_dir.exists()

    with db_session() as connection:
        rows = connection.execute(
            "SELECT apply_id FROM apply_versions ORDER BY apply_id"
        ).fetchall()
        compacted_job = connection.execute(
            "SELECT result_json FROM jobs WHERE job_id = ?",
            (job["job_id"],),
        ).fetchone()

    retained_apply_ids = {row["apply_id"] for row in rows}
    assert "11111111-1111-1111-1111-111111111111" not in retained_apply_ids
    assert "22222222-2222-2222-2222-222222222222" in retained_apply_ids
    assert len(retained_apply_ids) == 20
    compacted_payload = json.loads(compacted_job["result_json"])
    assert compacted_payload["__truncated__"] is True
    assert compacted_payload["summary"]["stage"] == "legacy"


def test_control_plane_maintenance_soft_deletes_xray_legacy_subscription_shadows(
    monkeypatch,
    tmp_path: Path,
) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()

    with db_session() as connection:
        connection.execute(
            """
            INSERT INTO subscription_accounts (account_id, slug, display_name, enabled)
            VALUES (1, 'stepan', 'Stepan', 1)
            """
        )
        connection.execute(
            """
            INSERT INTO subscription_clients (
                client_id, account_id, token, app_type, enabled, display_name, last_seen_at, last_user_agent
            )
            VALUES (1, 1, 'stepan', 'auto', 1, 'Stepan', CURRENT_TIMESTAMP, 'TestAgent')
            """
        )
        connection.execute(
            """
            INSERT INTO subjects (
                subject_id, subject_type, stable_key, display_name, desired_mode,
                runtime_state, is_active, last_seen_at, last_traffic_at
            ) VALUES
                ('xray:legacy-stepan', 'xray', 'xray:legacy-stepan', 'stepan', 'enabled', 'running', 0, CURRENT_TIMESTAMP, NULL),
                ('xray:active-stepan', 'xray', 'xray:active-stepan', 'stepan active', 'enabled', 'running', 1, CURRENT_TIMESTAMP, NULL),
                ('xray:traffic-stepan', 'xray', 'xray:traffic-stepan', 'stepan traffic', 'enabled', 'running', 0, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP),
                ('xray:portal', 'xray', 'xray:portal', 'portal', 'enabled', 'running', 0, CURRENT_TIMESTAMP, NULL)
            """
        )
        connection.execute(
            """
            INSERT INTO subject_xray (subject_id, client_id, client_uuid, email, enabled)
            VALUES
                ('xray:legacy-stepan', 'legacy-stepan', 'legacy-stepan', 'stepan@fwrouter.local', 1),
                ('xray:active-stepan', 'active-stepan', 'active-stepan', 'stepan@fwrouter.local', 1),
                ('xray:traffic-stepan', 'traffic-stepan', 'traffic-stepan', 'stepan@fwrouter.local', 1),
                ('xray:portal', 'portal', 'portal', 'portal@fwrouter.local', 1)
            """
        )

    dry_run = run_control_plane_maintenance(dry_run=True)
    assert dry_run["xray_legacy_shadows"]["candidates_count"] == 1
    assert dry_run["xray_legacy_shadows"]["soft_deleted_count"] == 0

    result = run_control_plane_maintenance(dry_run=False)
    assert result["xray_legacy_shadows"]["candidates_count"] == 1
    assert result["xray_legacy_shadows"]["soft_deleted_count"] == 1

    with db_session() as connection:
        rows = connection.execute(
            """
            SELECT subject_id, is_deleted, metadata_json
            FROM subjects
            WHERE subject_id IN (
                'xray:legacy-stepan',
                'xray:active-stepan',
                'xray:traffic-stepan',
                'xray:portal'
            )
            ORDER BY subject_id
            """
        ).fetchall()

    by_subject = {row["subject_id"]: dict(row) for row in rows}
    assert by_subject["xray:legacy-stepan"]["is_deleted"] == 1
    assert by_subject["xray:active-stepan"]["is_deleted"] == 0
    assert by_subject["xray:traffic-stepan"]["is_deleted"] == 0
    assert by_subject["xray:portal"]["is_deleted"] == 0

    metadata = json.loads(by_subject["xray:legacy-stepan"]["metadata_json"])
    assert metadata["cleanup"]["reason"] == "xray_legacy_subscription_shadow"
