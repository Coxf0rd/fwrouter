from __future__ import annotations
from _test_support import configure_test_state_dir as _configure_env

import json
import os
from pathlib import Path
import time

from fwrouter_api.db.connection import db_session, initialize_database
from fwrouter_api.core.config import get_settings
from fwrouter_api.services.jobs import create_job, mark_job_running, mark_job_success
from fwrouter_api.services.jobs_retention import cleanup_jobs_retention
from fwrouter_api.services.rules import get_rules_state
from fwrouter_api.jobs.manager import JobManager
from fwrouter_api.services.event_contract import current_event_context, reset_event_context, set_event_context




def _age_job(connection, job_id: str) -> None:
    connection.execute(
        "UPDATE jobs SET created_at = datetime('now', '-10 days'), "
        "finished_at = datetime('now', '-10 days'), updated_at = datetime('now', '-10 days') "
        "WHERE job_id = ?",
        (job_id,),
    )
    snapshot = connection.execute(
        "SELECT event_id, details_json FROM operational_logs WHERE event_type = ?",
        ("job.terminal_outcome",),
    ).fetchone()
    if snapshot is not None:
        details = json.loads(snapshot["details_json"])
        details["finished_at_utc"] = connection.execute(
            "SELECT finished_at FROM jobs WHERE job_id = ?", (job_id,),
        ).fetchone()["finished_at"]
        connection.execute(
            "UPDATE operational_logs SET details_json = ? WHERE event_id = ?",
            (json.dumps(details), snapshot["event_id"]),
        )


def test_cleanup_jobs_retention_clears_rules_state_job_references(
    monkeypatch,
    tmp_path: Path,
) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()

    job = create_job("rules_full_update", requested_by="pytest", input_data={"requested_by": "pytest"})
    artifact_dir = get_settings().paths.jobs_dir / job["job_id"]
    artifact_dir.mkdir(parents=True)
    (artifact_dir / "result.json").write_text("private result payload", encoding="utf-8")
    mark_job_running(job["job_id"])
    mark_job_success(job["job_id"], result={"ok": True})

    with db_session() as connection:
        _age_job(connection, job["job_id"])
        connection.execute(
            """
            UPDATE rules_state
            SET
                last_update_job_id = ?,
                last_apply_job_id = ?,
                status = 'success',
                updated_at = CURRENT_TIMESTAMP
            WHERE id = 1
            """,
            (job["job_id"], job["job_id"]),
        )

    result = cleanup_jobs_retention(dry_run=False)
    rules_state = get_rules_state()

    with db_session() as connection:
        snapshot = connection.execute(
            "SELECT details_json FROM operational_logs WHERE event_type = ?",
            ("job.terminal_outcome",),
        ).fetchone()

    assert result["deleted_jobs_count"] == 1, result
    assert not artifact_dir.exists()
    assert rules_state["last_update_job_id"] is None
    assert rules_state["last_apply_job_id"] is None
    assert snapshot is not None
    assert job["job_id"] in snapshot["details_json"]
    assert json.loads(snapshot["details_json"])["snapshot_assurance"] == "terminal_commit"


def test_retention_records_unverified_legacy_terminal_observation_before_delete(
    monkeypatch, tmp_path: Path,
) -> None:
    from fwrouter_api.services.api_client_attribution import (
        attribution_from_snapshot,
        set_api_request_id,
        reset_api_request_id,
    )

    _configure_env(monkeypatch, tmp_path)
    initialize_database()
    with attribution_from_snapshot({
        "assurance": "credential_bound", "verified": True,
        "client_id": "a" * 32, "credential_id": "b" * 32,
        "kind": "cli", "tag_snapshot": "old-client", "policy_revision": 1,
        "api_request_id": "origin-request",
    }):
        job = create_job("legacy", requested_by="pytest")
    mark_job_running(job["job_id"])
    mark_job_success(job["job_id"], result={"private": "old result"})
    with db_session() as connection:
        connection.execute(
            "DELETE FROM operational_logs WHERE event_type = ?",
            ("job.terminal_outcome",),
        )
        connection.execute(
            "UPDATE jobs SET created_at = datetime('now', '-10 days'), "
            "finished_at = datetime('now', '-10 days'), updated_at = datetime('now', '-10 days') "
            "WHERE job_id = ?",
            (job["job_id"],),
        )

    ambient_context = set_event_context(
        request_id="ambient-request", apply_id="ambient-apply", workflow_id="ambient-workflow",
    )
    api_request = set_api_request_id("ambient-request")
    try:
        with attribution_from_snapshot({
            "assurance": "credential_bound", "verified": True,
            "client_id": "c" * 32, "credential_id": "d" * 32,
            "kind": "cli", "tag_snapshot": "current-client", "policy_revision": 2,
            "api_request_id": "ambient-identity-request",
        }):
            result = cleanup_jobs_retention(dry_run=False)
    finally:
        reset_api_request_id(api_request)
        reset_event_context(ambient_context)

    with db_session() as connection:
        job_row = connection.execute("SELECT job_id FROM jobs WHERE job_id = ?", (job["job_id"],)).fetchone()
        event = connection.execute(
            "SELECT details_json, created_at FROM operational_logs WHERE event_type = ?",
            ("job.terminal_outcome",),
        ).fetchone()
    payload = json.loads(event["details_json"])
    assert result["deleted_jobs_count"] == 1
    assert job_row is None
    assert payload["snapshot_assurance"] == "retention_observation"
    assert payload["execution_classification"] == "legacy_terminal_observed"
    assert payload["execution_status"] == "success"
    assert "api_identity" not in payload and "api_request_id" not in payload
    assert payload["event_context"]["job_id"] == job["job_id"]
    assert "request_id" not in payload["event_context"]
    assert "apply_id" not in payload["event_context"]
    assert "workflow_id" not in payload["event_context"]
    assert event["created_at"] > "2026-10-18 00:00:00"


def test_retention_snapshot_failure_rolls_back_delete_and_retry_is_idempotent(
    monkeypatch, tmp_path: Path,
) -> None:
    import fwrouter_api.services.jobs as jobs_service

    _configure_env(monkeypatch, tmp_path)
    initialize_database()
    job = create_job("legacy")
    mark_job_running(job["job_id"])
    mark_job_success(job["job_id"], result={"ok": True})
    with db_session() as connection:
        connection.execute(
            "DELETE FROM operational_logs WHERE event_type = ?", ("job.terminal_outcome",),
        )
        _age_job(connection, job["job_id"])

    original_writer = jobs_service.write_audit_event

    def fail_writer(**_kwargs):
        raise RuntimeError("event storage unavailable")

    monkeypatch.setattr(jobs_service, "write_audit_event", fail_writer)
    failed = cleanup_jobs_retention(dry_run=False)
    with db_session() as connection:
        retained = connection.execute(
            "SELECT job_id FROM jobs WHERE job_id = ?", (job["job_id"],),
        ).fetchone()
        events = connection.execute(
            "SELECT COUNT(*) FROM operational_logs WHERE event_type = ?",
            ("job.terminal_outcome",),
        ).fetchone()[0]
    assert failed["deleted_jobs_count"] == 0
    assert retained is not None and events == 0

    monkeypatch.setattr(jobs_service, "write_audit_event", original_writer)
    retried = cleanup_jobs_retention(dry_run=False)
    retried_again = cleanup_jobs_retention(dry_run=False)
    with db_session() as connection:
        events = connection.execute(
            "SELECT COUNT(*) FROM operational_logs WHERE event_type = ?",
            ("job.terminal_outcome",),
        ).fetchone()[0]
    assert retried["deleted_jobs_count"] == 1
    assert retried_again["deleted_jobs_count"] == 0
    assert events == 1


def test_retention_database_rollback_preserves_orphans_until_retry(
    monkeypatch, tmp_path: Path,
) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()
    job = create_job("legacy")
    mark_job_running(job["job_id"])
    mark_job_success(job["job_id"], result={"ok": True})
    with db_session() as connection:
        connection.execute(
            "DELETE FROM operational_logs WHERE event_type = ?", ("job.terminal_outcome",),
        )
        _age_job(connection, job["job_id"])
        connection.execute(
            """CREATE TRIGGER reject_legacy_snapshot BEFORE INSERT ON operational_logs
               WHEN NEW.event_type = 'job.terminal_outcome'
               BEGIN SELECT RAISE(ABORT, 'test retention snapshot failure'); END"""
        )

    orphan_dir = get_settings().paths.jobs_dir / "orphan-snapshot-failure"
    orphan_dir.mkdir(parents=True)
    orphan_file = orphan_dir / "keep.txt"
    orphan_file.write_text("preserve until database retry", encoding="utf-8")
    old_time = time.time() - 2 * 86400
    os.utime(orphan_dir, (old_time, old_time))

    failed = cleanup_jobs_retention(dry_run=False)
    assert failed["deleted_jobs_count"] == 0
    assert any(error.get("error", "").startswith("retention_transaction_failed:")
               for error in failed["errors"])
    assert orphan_file.read_text(encoding="utf-8") == "preserve until database retry"
    with db_session() as connection:
        assert connection.execute(
            "SELECT 1 FROM jobs WHERE job_id = ?", (job["job_id"],),
        ).fetchone() is not None
        connection.execute("DROP TRIGGER reject_legacy_snapshot")

    retried = cleanup_jobs_retention(dry_run=False)
    assert retried["deleted_jobs_count"] == 1
    assert str(orphan_dir) in retried["deleted_artifact_dirs"]
    assert not orphan_dir.exists()


def test_cleanup_jobs_retention_preserves_apply_versions_fk_integrity(
    monkeypatch,
    tmp_path: Path,
) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()

    job = create_job("apply_mutation", requested_by="pytest", input_data={"requested_by": "pytest"})
    mark_job_running(job["job_id"])
    mark_job_success(job["job_id"], result={"ok": True})

    with db_session() as connection:
        _age_job(connection, job["job_id"])
        connection.execute(
            """
            INSERT INTO apply_versions (
                apply_id,
                job_id,
                manifest_path,
                created_at,
                status
            )
            VALUES (
                'apply-retention-fk',
                ?,
                '/tmp/apply-retention-fk.json',
                datetime('now', '-10 days'),
                'applied'
            )
            """,
            (job["job_id"],),
        )

    result = cleanup_jobs_retention(dry_run=False)

    with db_session() as connection:
        apply_version = connection.execute(
            "SELECT job_id FROM apply_versions WHERE apply_id = 'apply-retention-fk'"
        ).fetchone()
        fk_violations = connection.execute("PRAGMA foreign_key_check").fetchall()

    assert result["deleted_jobs_count"] == 1
    assert apply_version["job_id"] is None
    assert fk_violations == []


def test_nested_job_context_uses_child_job_id_and_preserves_parent_correlation(
    monkeypatch, tmp_path: Path,
) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()
    manager = JobManager()
    observed: dict[str, str] = {}

    def child_handler(_job):
        observed.update(current_event_context())
        return {"ok": True}

    manager.register_handler("nested_context_test", child_handler)
    parent_context = set_event_context(
        job_id="parent-job",
        workflow_id="fixed-workflow",
        causation_id="parent-request",
    )
    try:
        child = manager.create("nested_context_test", requested_by="pytest")
        finished = manager.start_job_and_wait(child["job_id"])
        assert finished is not None
        assert finished["status"] == "success"
        assert observed["job_id"] == child["job_id"]
        assert observed["workflow_id"] == "fixed-workflow"
        assert observed["causation_id"] == "parent-request"
        assert current_event_context()["job_id"] == "parent-job"
    finally:
        reset_event_context(parent_context)

    assert current_event_context() == {}
