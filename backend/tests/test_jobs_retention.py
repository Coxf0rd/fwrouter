from __future__ import annotations

from pathlib import Path

from fwrouter_api.core.config import get_settings
from fwrouter_api.db.connection import db_session, initialize_database
from fwrouter_api.services.jobs import create_job, mark_job_running, mark_job_success
from fwrouter_api.services.jobs_retention import cleanup_jobs_retention
from fwrouter_api.services.rules import get_rules_state


def _configure_env(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("FWROUTER_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("FWROUTER_DATABASE_URL", f"sqlite:///{tmp_path}/fwrouter.db")
    get_settings.cache_clear()


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
