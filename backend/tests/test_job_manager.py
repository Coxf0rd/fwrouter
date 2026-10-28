from __future__ import annotations
from fwrouter_api.core.config import get_settings
from fwrouter_api.db.connection import initialize_database


import json
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from fwrouter_api.db.connection import db_session
from fwrouter_api.jobs.manager import JobManager
from fwrouter_api.services.apply_orchestrator import (
    INTENT_SET_GLOBAL_MODE,
    submit_apply_mutation,
)
from fwrouter_api.services.jobs import (
    compact_oversized_job_results,
    create_job,
    get_active_lock_lease,
    get_job,
    JobLockConflictError,
    list_jobs,
    mark_job_running,
    mark_job_success,
    mark_job_failed,
)
from fwrouter_api.services.logs import list_technical_logs
from fwrouter_api.services.event_contract import current_event_context


def _configure_env(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("FWROUTER_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("FWROUTER_JOB_RUN_NOW_WAIT_TIMEOUT_SECONDS", "1")
    monkeypatch.setenv("FWROUTER_JOB_STALE_TIMEOUT_SECONDS", "5")
    get_settings.cache_clear()


def test_run_now_true_fast_handler_returns_completed(monkeypatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()
    manager = JobManager()
    manager.register_handler(
        "apply_mutation",
        lambda job: {
            "job_status": "success",
            "mutation": {
                "ok": True,
                "intent": job["input"]["intent"],
            },
        },
    )
    monkeypatch.setattr(
        "fwrouter_api.services.apply_orchestrator.get_default_job_manager",
        lambda: manager,
    )

    job = submit_apply_mutation(
        intent=INTENT_SET_GLOBAL_MODE,
        payload={"mode": "direct"},
        requested_by="pytest",
        run_now=True,
    )

    assert job["status"] == "success"
    assert job["result"]["mutation"]["ok"] is True


def test_run_now_true_hanging_handler_returns_running_after_bounded_wait(
    monkeypatch,
    tmp_path: Path,
) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()
    manager = JobManager()
    release = threading.Event()

    def _slow_handler(job):
        release.wait(timeout=5)
        return {"job_status": "success", "mutation": {"ok": True, "job_id": job["job_id"]}}

    manager.register_handler("apply_mutation", _slow_handler)
    monkeypatch.setattr(
        "fwrouter_api.services.apply_orchestrator.get_default_job_manager",
        lambda: manager,
    )

    started = time.monotonic()
    job = submit_apply_mutation(
        intent=INTENT_SET_GLOBAL_MODE,
        payload={"mode": "selective"},
        requested_by="pytest",
        run_now=True,
    )
    elapsed = time.monotonic() - started

    assert elapsed < 3
    assert job["status"] == "running"

    release.set()
    finished = manager.wait_for_job(job["job_id"], timeout_seconds=2)
    assert finished is not None
    assert finished["status"] == "success"


def test_stale_running_job_is_marked_failed(monkeypatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()
    manager = JobManager()
    manager.register_handler("noop", lambda job: {"job_status": "success"})

    job = manager.create("noop", requested_by="pytest")
    running = mark_job_running(job["job_id"])
    assert running is not None
    assert running["status"] == "running"

    with db_session() as connection:
        connection.execute(
            """
            UPDATE jobs
            SET started_at = datetime('now', '-5 minutes'),
                updated_at = datetime('now', '-5 minutes')
            WHERE job_id = ?            """,
            (job["job_id"],),
        )

    stale = manager.cleanup_stale_jobs()
    updated = get_job(job["job_id"])

    assert stale
    assert updated is not None
    assert updated["status"] == "failed"
    assert updated["error_code"] == "JOB_STALE_TIMEOUT"
    assert "stale running lease" in str(updated["error_message"])


def test_manager_create_runs_one_stale_cleanup_and_releases_stale_queued_lock(
    monkeypatch,
    tmp_path: Path,
) -> None:
    import fwrouter_api.services.jobs as jobs_service

    _configure_env(monkeypatch, tmp_path)
    initialize_database()
    manager = JobManager()
    manager.register_handler("noop", lambda job: {"job_status": "success"})
    cleanup_calls = 0
    original_cleanup = jobs_service.cleanup_stale_running_jobs

    def count_cleanup(*, stale_after_seconds=None):
        nonlocal cleanup_calls
        cleanup_calls += 1
        return original_cleanup(stale_after_seconds=stale_after_seconds)

    monkeypatch.setattr(jobs_service, "cleanup_stale_running_jobs", count_cleanup)
    first = manager.create("noop", requested_by="pytest")
    assert cleanup_calls == 1

    with db_session() as connection:
        connection.execute(
            "UPDATE jobs SET lock_key = 'apply', updated_at = datetime('now', '-5 minutes') WHERE job_id = ?",
            (first["job_id"],),
        )

    second = manager.create("noop", requested_by="pytest", lock_key="apply")
    previous = get_job(first["job_id"])
    with db_session() as connection:
        snapshot = connection.execute(
            "SELECT details_json FROM operational_logs WHERE event_type = ?",
            ("job.terminal_outcome",),
        ).fetchone()

    assert second["status"] == "queued"
    assert previous is not None
    assert previous["status"] == "failed"
    assert previous["error_code"] == "JOB_STALE_QUEUE_TIMEOUT"
    payload = json.loads(snapshot["details_json"])
    assert payload["execution_status"] == "failed"
    assert payload["execution_classification"] == "stale_queue_timeout"


def test_run_job_works_from_background_thread_without_main_thread_timeout_dependency(
    monkeypatch,
    tmp_path: Path,
) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()
    manager = JobManager()
    manager.register_handler("noop", lambda job: {"job_status": "success", "job_id": job["job_id"]})

    job = manager.create("noop", requested_by="pytest")
    result_holder: dict[str, object] = {}

    def _runner() -> None:
        result_holder["job"] = manager.run_job(job["job_id"])

    worker = threading.Thread(target=_runner, daemon=True)
    worker.start()
    worker.join(timeout=2)

    assert worker.is_alive() is False
    assert result_holder["job"]["status"] == "success"  # type: ignore[index]


def test_run_now_false_starts_background_job(monkeypatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()
    manager = JobManager()
    manager.register_handler("apply_mutation", lambda job: {"job_status": "success"})
    monkeypatch.setattr(
        "fwrouter_api.services.apply_orchestrator.get_default_job_manager",
        lambda: manager,
    )

    job = submit_apply_mutation(
        intent=INTENT_SET_GLOBAL_MODE,
        payload={"mode": "direct"},
        requested_by="pytest",
        run_now=False,
    )

    assert job["status"] in {"running", "success"}
    final = manager.wait_for_job(job["job_id"], timeout_seconds=2)
    assert final is not None
    assert final["status"] == "success"


def test_list_jobs_cleans_up_stale_running_rows(monkeypatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()
    manager = JobManager()
    manager.register_handler("noop", lambda job: {"job_status": "success"})

    job = manager.create("noop", requested_by="pytest")
    running = mark_job_running(job["job_id"])
    assert running is not None

    with db_session() as connection:
        connection.execute(
            """
            UPDATE jobs
            SET started_at = datetime('now', '-5 minutes'),
                updated_at = datetime('now', '-5 minutes')
            WHERE job_id = ?            """,
            (job["job_id"],),
        )

    jobs = list_jobs(limit=10)
    stale_job = next(item for item in jobs if item["job_id"] == job["job_id"])

    assert stale_job["status"] == "failed"
    assert stale_job["error_code"] == "JOB_STALE_TIMEOUT"


def test_active_lock_lease_reports_owner_metadata(monkeypatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()
    manager = JobManager()
    manager.register_handler("noop", lambda job: {"job_status": "success"})

    job = manager.create("noop", requested_by="pytest", lock_key="apply")
    running = mark_job_running(job["job_id"])
    assert running is not None

    lease = get_active_lock_lease("apply")
    assert lease is not None
    assert lease["owner_job_id"] == job["job_id"]
    assert lease["owner_status"] == "running"
    assert lease["heartbeat_at"] is not None


def test_http_request_context_is_restored_in_async_job_worker(monkeypatch, tmp_path: Path) -> None:
    """Request correlation survives JobManager's thread boundary without entering job input."""
    from fastapi.testclient import TestClient

    from fwrouter_api.main import create_app
    from fwrouter_api.services.logs import write_technical_log

    _configure_env(monkeypatch, tmp_path)
    initialize_database()
    manager = JobManager()

    def _handler(job):
        write_technical_log(
            component="test-worker",
            level="info",
            event_type="job_context_probe",
            message="Worker context probe.",
            details={"job_id": job["job_id"]},
        )
        return {"job_status": "success"}

    manager.register_handler("context_probe", _handler)
    app = create_app(enable_startup_tasks=False)

    @app.post("/test/context-job")
    def _create_context_job():
        job = manager.create("context_probe", input_data={"safe": "value"})
        manager.start_job(job["job_id"])
        return {"job_id": job["job_id"]}

    response = TestClient(app).post(
        "/test/context-job", headers={"X-Request-ID": "request-correlation-42"}
    )
    assert response.status_code == 200
    assert response.headers["x-request-id"] == "request-correlation-42"

    job = manager.wait_for_job(response.json()["job_id"], timeout_seconds=2)
    assert job is not None and job["status"] == "success"
    assert job["input"] == {"safe": "value"}
    assert job["event_context"]["request_id"] == "request-correlation-42"
    assert job["event_context"]["correlation_id"] == "request-correlation-42"

    events = list_technical_logs(limit=20)
    correlated = next(item for item in events if item["event_type"] == "job_context_probe")
    assert correlated["details"]["request_id"] == "request-correlation-42"
    assert correlated["details"]["job_id"] == job["job_id"]
    assert correlated["details"]["workflow_id"] == job["job_id"]
    assert current_event_context() == {}


def test_active_lock_is_enforced_atomically(monkeypatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()
    manager = JobManager()
    barrier = threading.Barrier(2)
    created: list[dict[str, object]] = []
    conflicts: list[JobLockConflictError] = []

    def _create_locked_job() -> None:
        barrier.wait(timeout=2)
        try:
            created.append(manager.create("noop", requested_by="pytest", lock_key="apply"))
        except JobLockConflictError as exc:
            conflicts.append(exc)

    threads = [threading.Thread(target=_create_locked_job) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=2)

    assert len(created) == 1
    assert len(conflicts) == 1
    assert conflicts[0].lock_key == "apply"


def test_stale_running_apply_lock_does_not_block_new_apply_job(monkeypatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()
    manager = JobManager()
    manager.register_handler("apply_mutation", lambda job: {"job_status": "success"})
    monkeypatch.setattr(
        "fwrouter_api.services.apply_orchestrator.get_default_job_manager",
        lambda: manager,
    )

    stale = manager.create(
        "apply_mutation",
        requested_by="pytest",
        lock_key="apply",
        input_data={"intent": INTENT_SET_GLOBAL_MODE, "payload": {"mode": "vpn"}},
    )
    running = mark_job_running(stale["job_id"])
    assert running is not None

    with db_session() as connection:
        connection.execute(
            """
            UPDATE jobs
            SET started_at = datetime('now', '-5 minutes'),
                updated_at = datetime('now', '-5 minutes')
            WHERE job_id = ?            """,
            (stale["job_id"],),
        )

    job = submit_apply_mutation(
        intent=INTENT_SET_GLOBAL_MODE,
        payload={"mode": "direct"},
        requested_by="pytest",
        run_now=False,
    )

    updated_stale = get_job(stale["job_id"])
    assert updated_stale is not None
    assert updated_stale["status"] == "failed"
    assert updated_stale["error_code"] == "JOB_STALE_TIMEOUT"
    assert job["status"] in {"running", "success"}


def test_job_result_is_truncated_when_it_exceeds_storage_limit(monkeypatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    monkeypatch.setenv("FWROUTER_JOB_RESULT_MAX_BYTES", "4096")
    get_settings.cache_clear()
    initialize_database()
    manager = JobManager()

    job = manager.create("noop", requested_by="pytest")
    mark_job_running(job["job_id"])
    payload = {"job_status": "success", "stage": "huge", "blob": "x" * 20000}
    finished = mark_job_success(job["job_id"], result=payload)

    assert finished is not None
    assert finished["status"] == "success"
    assert finished["result"]["__truncated__"] is True
    assert finished["result"]["summary"]["stage"] == "huge"


def test_compact_oversized_job_results_rewrites_existing_rows(monkeypatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    monkeypatch.setenv("FWROUTER_JOB_RESULT_MAX_BYTES", "4096")
    get_settings.cache_clear()
    initialize_database()
    manager = JobManager()

    job = manager.create("noop", requested_by="pytest")
    mark_job_running(job["job_id"])
    mark_job_success(job["job_id"], result={"job_status": "success", "blob": "x" * 1000})

    with db_session() as connection:
        connection.execute(
            "UPDATE jobs SET result_json = ? WHERE job_id = ?",
            (json.dumps({"job_status": "success", "stage": "legacy", "blob": "y" * 20000}), job["job_id"]),
        )

    compacted = compact_oversized_job_results(dry_run=False)
    updated = get_job(job["job_id"])

    assert compacted["updated_jobs_count"] == 1
    assert updated is not None
    assert updated["result"]["__truncated__"] is True
    assert updated["result"]["summary"]["stage"] == "legacy"


def test_terminal_job_snapshot_is_single_safe_and_retained_after_repeat_calls(
    monkeypatch, tmp_path: Path,
) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()
    manager = JobManager()
    job = manager.create("noop", requested_by="pytest", input_data={"secret": "never-store"})
    mark_job_running(job["job_id"])
    finished = mark_job_success(
        job["job_id"],
        result={"secret": "result-secret", "path": "/private/path", "ok": True},
    )
    repeated = mark_job_failed(
        job["job_id"], error_code="ARBITRARY_SECRET_CODE", error_message="secret text",
    )

    with db_session() as connection:
        rows = connection.execute(
            "SELECT event_type, message, details_json FROM operational_logs WHERE event_type = ?",
            ("job.terminal_outcome",),
        ).fetchall()

    assert finished is not None and finished["status"] == "success"
    assert repeated is not None and repeated["status"] == "success"
    assert len(rows) == 1
    payload = json.loads(rows[0]["details_json"])
    assert payload["execution_status"] == "success"
    assert payload["execution_classification"] == "handler_completed"
    assert payload["job_id"] == job["job_id"]
    assert "secret" not in rows[0]["message"] + rows[0]["details_json"]
    assert "/private/path" not in rows[0]["details_json"]
    assert "ARBITRARY_SECRET_CODE" not in rows[0]["details_json"]


def test_handler_reported_cancellation_is_terminal_not_success(monkeypatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()
    manager = JobManager()
    manager.register_handler("cancelled", lambda _job: {"job_status": "cancelled"})
    job = manager.create("cancelled", requested_by="pytest")

    finished = manager.run_job(job["job_id"])
    with db_session() as connection:
        event = connection.execute(
            "SELECT details_json FROM operational_logs WHERE event_type = ?",
            ("job.terminal_outcome",),
        ).fetchone()
    payload = json.loads(event["details_json"])

    assert finished is not None and finished["status"] == "cancelled"
    assert payload["outcome"] == "cancelled"
    assert payload["execution_status"] == "cancelled"
    assert payload["execution_classification"] == "handler_cancelled"
    assert payload["snapshot_assurance"] == "terminal_commit"


def test_terminal_snapshot_failure_rolls_back_job_transition(monkeypatch, tmp_path: Path) -> None:
    import pytest
    import fwrouter_api.services.jobs as jobs_service

    _configure_env(monkeypatch, tmp_path)
    initialize_database()
    job = create_job("noop")
    mark_job_running(job["job_id"])

    def fail_snapshot(**_kwargs):
        raise RuntimeError("audit storage unavailable")

    monkeypatch.setattr(jobs_service, "write_audit_event", fail_snapshot)
    with pytest.raises(RuntimeError, match="audit storage unavailable"):
        mark_job_success(job["job_id"], result={"ok": True})

    current = get_job(job["job_id"])
    assert current is not None and current["status"] == "running"


def test_sqlite_terminal_event_insert_abort_rolls_back_and_retry_succeeds(
    monkeypatch, tmp_path: Path,
) -> None:
    import pytest

    _configure_env(monkeypatch, tmp_path)
    initialize_database()
    job = create_job("terminal_insert_abort_probe")
    mark_job_running(job["job_id"])
    with db_session() as connection:
        connection.execute(
            """CREATE TRIGGER reject_terminal_snapshot BEFORE INSERT ON operational_logs
               WHEN NEW.event_type = 'job.terminal_outcome'
               BEGIN SELECT RAISE(ABORT, 'test terminal event insert failure'); END"""
        )
    try:
        with pytest.raises(sqlite3.IntegrityError, match="test terminal event insert failure"):
            mark_job_success(job["job_id"], result={"ok": True})
        with db_session() as connection:
            assert connection.execute(
                "SELECT status FROM jobs WHERE job_id = ?", (job["job_id"],)
            ).fetchone()[0] == "running"
            assert connection.execute(
                "SELECT COUNT(*) FROM operational_logs WHERE event_type = 'job.terminal_outcome'"
            ).fetchone()[0] == 0
            connection.execute("DROP TRIGGER reject_terminal_snapshot")
        assert mark_job_success(job["job_id"], result={"ok": True})["status"] == "success"
        with db_session() as connection:
            assert connection.execute(
                "SELECT COUNT(*) FROM operational_logs WHERE event_type = 'job.terminal_outcome'"
            ).fetchone()[0] == 1
    finally:
        with db_session() as connection:
            connection.execute("DROP TRIGGER IF EXISTS reject_terminal_snapshot")


def test_concurrent_terminal_calls_and_stale_cleanup_emit_only_winning_snapshot(
    monkeypatch, tmp_path: Path,
) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()
    job = create_job("noop")
    mark_job_running(job["job_id"])
    with db_session() as connection:
        connection.execute(
            "UPDATE jobs SET updated_at = datetime('now', '-5 minutes') WHERE job_id = ?",
            (job["job_id"],),
        )
    barrier = threading.Barrier(3)

    def complete():
        barrier.wait()
        return mark_job_success(job["job_id"], result={"ok": True})

    def timeout():
        barrier.wait()
        from fwrouter_api.services.jobs import cleanup_stale_running_jobs
        return cleanup_stale_running_jobs(stale_after_seconds=1)

    with ThreadPoolExecutor(max_workers=2) as pool:
        success_future = pool.submit(complete)
        stale_future = pool.submit(timeout)
        barrier.wait()
        success_future.result(timeout=5)
        stale_future.result(timeout=5)

    final = get_job(job["job_id"])
    with db_session() as connection:
        snapshots = connection.execute(
            "SELECT details_json FROM operational_logs WHERE event_type = ?",
            ("job.terminal_outcome",),
        ).fetchall()
    assert final is not None and final["status"] in {"success", "failed"}
    assert len(snapshots) == 1
    assert json.loads(snapshots[0]["details_json"])["execution_status"] == final["status"]


def test_stale_job_snapshot_uses_original_job_correlation(monkeypatch, tmp_path: Path) -> None:
    from fwrouter_api.services.event_contract import reset_event_context, set_event_context
    from fwrouter_api.services.api_client_attribution import (
        attribution_from_snapshot, reset_api_request_id, set_api_request_id,
    )

    _configure_env(monkeypatch, tmp_path)
    initialize_database()
    manager = JobManager()
    marker = set_event_context(request_id="origin-request", workflow_id="origin-workflow")
    try:
        with attribution_from_snapshot({
            "assurance": "credential_bound", "verified": True,
            "client_id": "a" * 32, "credential_id": "b" * 32,
            "kind": "cli", "tag_snapshot": "origin-client", "policy_revision": 1,
            "api_request_id": "origin-api-request",
        }):
            job = manager.create("noop", requested_by="pytest")
    finally:
        reset_event_context(marker)
    mark_job_running(job["job_id"])
    with db_session() as connection:
        connection.execute(
            "UPDATE jobs SET updated_at = datetime('now', '-5 minutes') WHERE job_id = ?",
            (job["job_id"],),
        )

    ambient_context = set_event_context(
        request_id="ambient-request", apply_id="ambient-apply", workflow_id="ambient-workflow",
    )
    ambient_api_request = set_api_request_id("ambient-api-request")
    try:
        with attribution_from_snapshot({
            "assurance": "credential_bound", "verified": True,
            "client_id": "c" * 32, "credential_id": "d" * 32,
            "kind": "cli", "tag_snapshot": "ambient-client", "policy_revision": 2,
            "api_request_id": "ambient-identity-request",
        }):
            stale = manager.cleanup_stale_jobs()
    finally:
        reset_api_request_id(ambient_api_request)
        reset_event_context(ambient_context)
    with db_session() as connection:
        row = connection.execute(
            "SELECT details_json FROM operational_logs WHERE event_type = ?",
            ("job.terminal_outcome",),
        ).fetchone()
    payload = json.loads(row["details_json"])

    assert stale[0]["status"] == "failed"
    assert payload["execution_classification"] == "stale_running_timeout"
    assert payload["event_context"]["request_id"] == "origin-request"
    assert payload["event_context"]["workflow_id"] == "origin-workflow"
    assert "apply_id" not in payload["event_context"]
    assert payload["api_identity"]["api_client_id"] == "a" * 32
    assert payload["api_identity"]["api_request_id"] == "origin-api-request"
