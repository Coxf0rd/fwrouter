from __future__ import annotations
from fwrouter_api.core.config import get_settings
from fwrouter_api.db.connection import initialize_database


import json
import threading
import time
from pathlib import Path

from fwrouter_api.db.connection import db_session, initialize_database
from fwrouter_api.jobs.manager import JobManager
from fwrouter_api.services.apply_orchestrator import (
    INTENT_SET_GLOBAL_MODE,
    submit_apply_mutation,
)
from fwrouter_api.services.jobs import (
    compact_oversized_job_results,
    get_active_lock_lease,
    get_job,
    list_jobs,
    mark_job_running,
    mark_job_success,
)


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
