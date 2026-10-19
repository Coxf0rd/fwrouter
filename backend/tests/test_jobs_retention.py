from __future__ import annotations
from _test_support import configure_test_state_dir as _configure_env

from pathlib import Path

from fwrouter_api.db.connection import db_session, initialize_database
from fwrouter_api.services.jobs import create_job, mark_job_running, mark_job_success
from fwrouter_api.services.jobs_retention import cleanup_jobs_retention
from fwrouter_api.services.rules import get_rules_state
from fwrouter_api.jobs.manager import JobManager
from fwrouter_api.services.event_contract import current_event_context, reset_event_context, set_event_context




def test_cleanup_jobs_retention_clears_rules_state_job_references(
    monkeypatch,
    tmp_path: Path,
) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()

    job = create_job("rules_full_update", requested_by="pytest", input_data={"requested_by": "pytest"})
    mark_job_running(job["job_id"])
    mark_job_success(job["job_id"], result={"ok": True})

    with db_session() as connection:
        connection.execute(
            """
            UPDATE jobs
            SET created_at = datetime('now', '-10 days'),
                finished_at = datetime('now', '-10 days'),
                updated_at = datetime('now', '-10 days')
            WHERE job_id = ?
            """,
            (job["job_id"],),
        )
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

    assert result["deleted_jobs_count"] == 1
    assert rules_state["last_update_job_id"] is None
    assert rules_state["last_apply_job_id"] is None


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
        connection.execute(
            """
            UPDATE jobs
            SET created_at = datetime('now', '-10 days'),
                finished_at = datetime('now', '-10 days'),
                updated_at = datetime('now', '-10 days')
            WHERE job_id = ?
            """,
            (job["job_id"],),
        )
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
