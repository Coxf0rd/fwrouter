from __future__ import annotations

import asyncio
import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace

from fwrouter_api import main


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
    monkeypatch.setattr(main, "get_default_job_manager", lambda: object())

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
        monkeypatch.setattr(main, name, lambda: None)
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
