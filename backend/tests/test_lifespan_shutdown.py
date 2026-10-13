from __future__ import annotations

import asyncio
from types import SimpleNamespace

from fwrouter_api import main


def test_lifespan_signals_all_components_before_shared_join_budget(monkeypatch) -> None:
    paths = SimpleNamespace(operational_log_dir="/tmp/unused-op", technical_log_dir="/tmp/unused-tech")
    settings = SimpleNamespace(app_name="test", app_version="test", debug=False, paths=paths)
    monkeypatch.setattr(main, "get_settings", lambda: settings)

    monkeypatch.setattr(main, "resume_runtime_read_models_prewarm", lambda: True)
    startup_calls: list[str] = []
    monkeypatch.setattr(main, "bootstrap_backend", lambda: startup_calls.append("bootstrap"))
    monkeypatch.setattr(main, "scrub_jsonl_files", lambda _paths: startup_calls.append("scrub"))
    monkeypatch.setattr(
        main,
        "register_extended_handlers",
        lambda _manager: startup_calls.append("register_handlers"),
    )
    monkeypatch.setattr(main, "get_default_job_manager", lambda: object())

    start_names = (
        "start_maintenance_scheduler",
        "start_member_probe_scheduler",
        "start_active_observation_scheduler",
        "start_subject_inventory_scheduler",
        "start_external_collector_scheduler",
        "start_runtime_convergence_scheduler",
        "start_watchdog_scheduler",
        "prime_runtime_read_models_async",
    )
    for name in start_names:
        monkeypatch.setattr(
            main,
            name,
            lambda name=name, **_kwargs: startup_calls.append(name),
        )

    stop_names = (
        "stop_maintenance_scheduler",
        "stop_member_probe_scheduler",
        "stop_active_observation_scheduler",
        "stop_subject_inventory_scheduler",
        "stop_external_collector_scheduler",
        "stop_runtime_convergence_scheduler",
        "stop_watchdog_scheduler",
        "stop_runtime_read_models_prewarm",
    )
    stop_calls: list[tuple[str, int, float]] = []
    for name in stop_names:
        def make_stop(stop_name: str):
            calls = 0

            def stop(*, timeout_seconds: float) -> None:
                nonlocal calls
                calls += 1
                stop_calls.append((stop_name, calls, timeout_seconds))

            return stop

        monkeypatch.setattr(main, name, make_stop(name))

    current_time = 0.0

    def monotonic() -> float:
        nonlocal current_time
        value = current_time
        current_time += 0.25
        return value

    monkeypatch.setattr(main, "monotonic", monotonic)
    app = main.create_app(enable_startup_tasks=True)

    async def exercise_lifespan() -> None:
        async with app.router.lifespan_context(app):
            pass

    asyncio.run(exercise_lifespan())

    # bootstrap_backend is mocked here: this verifies lifecycle wiring only,
    # with no claim that native runtimes or dataplane were started or checked.
    assert startup_calls == [
        "bootstrap",
        "scrub",
        "register_handlers",
        *start_names,
    ]
    assert [name for name, _call, _timeout in stop_calls[: len(stop_names)]] == list(stop_names)
    assert all(timeout == 0.0 for _name, _call, timeout in stop_calls[: len(stop_names)])
    assert [name for name, _call, _timeout in stop_calls[len(stop_names) :]] == list(stop_names)
    join_timeouts = [timeout for _name, _call, timeout in stop_calls[len(stop_names) :]]
    assert join_timeouts == [13.75, 13.5, 13.25, 13.0, 12.75, 12.5, 12.25, 12.0]
    assert all(0.0 <= timeout <= 14.0 for timeout in join_timeouts)
