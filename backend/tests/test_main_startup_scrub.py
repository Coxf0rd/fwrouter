from __future__ import annotations

import asyncio
import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace

from fastapi.testclient import TestClient

from fwrouter_api import main
from fwrouter_api.db.connection import db_session
from fwrouter_api.jobs.manager import get_default_job_manager


def test_v21_startup_scrubs_configured_jsonl_before_schedulers(
    monkeypatch, tmp_path: Path
) -> None:
    operational_dir = tmp_path / "operational"
    technical_dir = tmp_path / "technical"
    operational_dir.mkdir()
    technical_dir.mkdir()
    event_path = operational_dir / "events.jsonl"
    sentinel = "startup-scrub-credential-sentinel"
    event_path.write_text(
        json.dumps({
            "event_id": "historical-event-1",
            "event_type": "xray_client_created",
            "details": {"subscription_uri": sentinel},
        }) + "\n",
        encoding="utf-8",
    )

    db_path = tmp_path / "already-current.db"
    with sqlite3.connect(db_path) as connection:
        connection.execute("CREATE TABLE schema_meta (key TEXT PRIMARY KEY, value TEXT)")
        connection.execute(
            "INSERT INTO schema_meta (key, value) VALUES ('schema_version', '21')"
        )

    paths = SimpleNamespace(
        operational_log_dir=operational_dir,
        technical_log_dir=technical_dir,
    )
    settings = SimpleNamespace(
        app_name="test",
        app_version="test",
        debug=False,
        paths=paths,
    )
    monkeypatch.setattr(main, "get_settings", lambda: settings)

    sequence: list[str] = []

    def bootstrap() -> None:
        with sqlite3.connect(db_path) as connection:
            version = connection.execute(
                "SELECT value FROM schema_meta WHERE key = 'schema_version'"
            ).fetchone()[0]
        assert version == "21"
        sequence.append("bootstrap")

    actual_scrub = main.scrub_jsonl_files

    def scrub(paths_to_scrub):
        result = actual_scrub(paths_to_scrub)
        sequence.append("scrub")
        return result

    monkeypatch.setattr(main, "bootstrap_backend", bootstrap)
    monkeypatch.setattr(main, "scrub_jsonl_files", scrub)
    monkeypatch.setattr(
        main,
        "register_extended_handlers",
        lambda _manager: sequence.append("register_handlers"),
    )
    monkeypatch.setattr(main, "get_default_job_manager",
                        lambda: SimpleNamespace(wait_for_idle=lambda **_kwargs: True))
    monkeypatch.setattr(main, "resume_runtime_read_models_prewarm", lambda: True)
    monkeypatch.setattr(main, "stop_runtime_read_models_prewarm", lambda **_kwargs: True)

    for name in (
        "start_maintenance_scheduler",
        "start_member_probe_scheduler",
        "start_active_observation_scheduler",
        "start_subject_inventory_scheduler",
        "start_external_collector_scheduler",
        "start_runtime_convergence_scheduler",
        "start_watchdog_scheduler",
    ):
        monkeypatch.setattr(main, name, lambda name=name: sequence.append(name))
    for name in (
        "stop_maintenance_scheduler",
        "stop_member_probe_scheduler",
        "stop_active_observation_scheduler",
        "stop_subject_inventory_scheduler",
        "stop_external_collector_scheduler",
        "stop_runtime_convergence_scheduler",
        "stop_watchdog_scheduler",
    ):
        monkeypatch.setattr(main, name, lambda **_kwargs: None)
    monkeypatch.setattr(
        main,
        "prime_runtime_read_models_async",
        lambda **_kwargs: sequence.append("prime_read_models"),
    )

    app = main.create_app(enable_startup_tasks=True)

    async def exercise_lifespan() -> None:
        async with app.router.lifespan_context(app):
            assert sequence.index("scrub") < sequence.index("start_maintenance_scheduler")

    asyncio.run(exercise_lifespan())

    record = json.loads(event_path.read_text(encoding="utf-8"))
    assert record["event_id"] == "historical-event-1"
    assert record["details"]["subscription_uri"] == "[REDACTED]"
    assert sentinel not in event_path.read_text(encoding="utf-8")
    assert sequence[:3] == ["bootstrap", "scrub", "register_handlers"]
    assert "start_maintenance_scheduler" in sequence


def test_maintenance_lifespan_skips_bootstrap_registration_and_background_workers(
    monkeypatch,
) -> None:
    """The explicit maintenance API mode exposes routes without startup work."""
    sequence: list[str] = []

    def forbidden(name: str):
        def fail(*_args, **_kwargs):
            sequence.append(name)
            raise AssertionError(f"maintenance lifespan invoked {name}")
        return fail

    for name in (
        "bootstrap_backend", "scrub_jsonl_files", "register_extended_handlers",
        "start_maintenance_scheduler", "start_member_probe_scheduler",
        "start_active_observation_scheduler", "start_subject_inventory_scheduler",
        "start_external_collector_scheduler", "start_runtime_convergence_scheduler",
        "start_watchdog_scheduler", "resume_runtime_read_models_prewarm",
        "prime_runtime_read_models_async",
    ):
        monkeypatch.setattr(main, name, forbidden(name))
    for name in (
        "stop_maintenance_scheduler", "stop_member_probe_scheduler",
        "stop_active_observation_scheduler", "stop_subject_inventory_scheduler",
        "stop_external_collector_scheduler", "stop_runtime_convergence_scheduler",
        "stop_watchdog_scheduler",
    ):
        monkeypatch.setattr(main, name, forbidden(name))

    app = main.create_app(enable_startup_tasks=False)
    route_paths = {route.path for route in app.routes}
    assert "/api/v2/modules/{module_name}/desired-state" in route_paths
    assert "/api/v2/routing/global" in route_paths

    async def exercise_lifespan() -> None:
        async with app.router.lifespan_context(app):
            assert sequence == []

    asyncio.run(exercise_lifespan())
    assert sequence == []

    # Exercise the real route and lazy default registration. The owned unit
    # harness denies process execution before any dataplane/native operation;
    # that expected failure proves the real handler ran without mocking it.
    with TestClient(app) as client:
        response = client.post(
            "/api/v2/routing/global",
            json={"mode": "direct", "requested_by": "pytest-maintenance", "run_now": True},
        )
    assert response.status_code == 200
    response_body = response.json()
    job = response_body.get("data", {}).get("job", {})
    assert response_body.get("ok") is False, response_body
    assert job.get("job_type") == "apply_mutation", response_body
    job_id = job.get("job_id")
    assert isinstance(job_id, str) and job_id, response_body
    assert response_body.get("error", {}).get("code") == "JOB_HANDLER_FAILED", response_body
    assert response_body.get("error", {}).get("job_type") == "apply_mutation", response_body
    assert response_body.get("error", {}).get("job_id") == job_id, response_body
    assert "tests deny process execution" in response_body.get("error", {}).get("message", ""), response_body
    manager = get_default_job_manager()
    assert manager._get_handler("apply_mutation") is not None
    with db_session() as connection:
        finished_job = connection.execute(
            "SELECT job_type, status, error_code, error_message FROM jobs WHERE job_id=?",
            (job_id,),
        ).fetchone()
    assert finished_job is not None
    assert finished_job[0] == "apply_mutation", tuple(finished_job)
    assert finished_job[1] == "failed", tuple(finished_job)
    assert finished_job[2] == "JOB_HANDLER_FAILED", tuple(finished_job)
    assert "tests deny process execution" in str(finished_job[3])
