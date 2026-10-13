from __future__ import annotations

from threading import Event

from fwrouter_api.services import runtime_prewarm


def test_ordinary_component_fixture_rejects_incidental_prewarm_thread(monkeypatch) -> None:
    assert runtime_prewarm._PREWARM_RUNNING is True
    thread_constructions: list[object] = []

    def unexpected_thread(*args, **kwargs):
        thread_constructions.append((args, kwargs))
        raise AssertionError("ordinary component tests must not start an incidental prewarm thread")

    monkeypatch.setattr(runtime_prewarm, "Thread", unexpected_thread)

    assert runtime_prewarm.prime_runtime_read_models_async() is False
    assert thread_constructions == []


def test_controlled_prewarm_admits_one_worker_and_rejects_concurrent_request(
    controlled_runtime_prewarm,
    monkeypatch,
) -> None:
    started = Event()
    release = Event()
    calls: list[dict[str, object]] = []

    def blocked_warm(*, include_global_profiles: bool = True) -> None:
        calls.append({"include_global_profiles": include_global_profiles})
        started.set()
        if not release.wait(timeout=4.0):
            raise AssertionError("test did not release the controlled prewarm worker")

    monkeypatch.setattr(runtime_prewarm, "warm_runtime_read_models", blocked_warm)

    try:
        assert runtime_prewarm.prime_runtime_read_models_async(include_global_profiles=False) is True
        assert started.wait(timeout=2.0)
        assert runtime_prewarm.prime_runtime_read_models_async() is False
        assert len(controlled_runtime_prewarm) == 1
    finally:
        release.set()

    controlled_runtime_prewarm[0].join(timeout=5.0)
    assert not controlled_runtime_prewarm[0].is_alive()
    assert calls == [{"include_global_profiles": False}]
    assert runtime_prewarm._PREWARM_RUNNING is False


def test_controlled_prewarm_releases_admission_after_worker_exception(
    controlled_runtime_prewarm,
    monkeypatch,
) -> None:
    calls: list[int] = []

    def warm_with_one_failure(*, include_global_profiles: bool = True) -> None:
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("synthetic read-model failure")

    monkeypatch.setattr(runtime_prewarm, "warm_runtime_read_models", warm_with_one_failure)

    assert runtime_prewarm.prime_runtime_read_models_async() is True
    controlled_runtime_prewarm[0].join(timeout=5.0)
    assert not controlled_runtime_prewarm[0].is_alive()
    assert len(controlled_runtime_prewarm) == 1
    assert runtime_prewarm._PREWARM_RUNNING is False

    assert runtime_prewarm.prime_runtime_read_models_async() is True
    controlled_runtime_prewarm[1].join(timeout=5.0)
    assert not controlled_runtime_prewarm[1].is_alive()
    assert len(controlled_runtime_prewarm) == 2
    assert runtime_prewarm._PREWARM_RUNNING is False
    assert calls == [1, 1]


def test_warm_runtime_read_models_invokes_each_read_model_once(monkeypatch) -> None:
    from fwrouter_api.services import global_mode_profiles, runtime, system_summary, ui_state

    calls: list[str] = []
    monkeypatch.setattr(runtime, "get_runtime_summary", lambda: calls.append("runtime"))
    monkeypatch.setattr(system_summary, "build_system_summary", lambda: calls.append("system"))
    monkeypatch.setattr(ui_state, "list_ui_clients", lambda: calls.append("clients"))
    monkeypatch.setattr(ui_state, "get_ui_router_summary", lambda: calls.append("router"))
    monkeypatch.setattr(ui_state, "get_ui_settings_workspace", lambda: calls.append("settings"))
    monkeypatch.setattr(
        global_mode_profiles,
        "compile_all_global_mode_profiles",
        lambda: calls.append("profiles"),
    )

    runtime_prewarm.warm_runtime_read_models(include_global_profiles=True)

    assert calls == ["runtime", "system", "clients", "router", "settings", "profiles"]


def test_prewarm_stop_cancels_between_read_model_projections(
    controlled_runtime_prewarm,
    monkeypatch,
) -> None:
    from fwrouter_api.services import global_mode_profiles, runtime, system_summary, ui_state

    entered_projection = Event()
    release_projection = Event()
    calls: list[str] = []

    def blocked_runtime_projection() -> None:
        calls.append("runtime")
        entered_projection.set()
        if not release_projection.wait(timeout=4.0):
            raise AssertionError("test did not release the prewarm projection barrier")

    monkeypatch.setattr(runtime, "get_runtime_summary", blocked_runtime_projection)
    monkeypatch.setattr(system_summary, "build_system_summary", lambda: calls.append("system"))
    monkeypatch.setattr(ui_state, "list_ui_clients", lambda: calls.append("clients"))
    monkeypatch.setattr(ui_state, "get_ui_router_summary", lambda: calls.append("router"))
    monkeypatch.setattr(ui_state, "get_ui_settings_workspace", lambda: calls.append("settings"))
    monkeypatch.setattr(
        global_mode_profiles,
        "compile_all_global_mode_profiles",
        lambda: calls.append("profiles"),
    )

    try:
        assert runtime_prewarm.prime_runtime_read_models_async(include_global_profiles=True)
        assert entered_projection.wait(timeout=2.0)
        assert runtime_prewarm.stop_runtime_read_models_prewarm(timeout_seconds=0.0) is False
        release_projection.set()
        assert runtime_prewarm.stop_runtime_read_models_prewarm(timeout_seconds=2.0) is True
        assert not controlled_runtime_prewarm[0].is_alive()
        assert calls == ["runtime"]
        assert runtime_prewarm._PREWARM_RUNNING is False
    finally:
        release_projection.set()
        for worker in controlled_runtime_prewarm:
            worker.join(timeout=2.0)
        runtime_prewarm._PREWARM_STOP.clear()


def test_shutdown_closes_prewarm_admission_until_next_lifespan(monkeypatch) -> None:
    monkeypatch.setattr(runtime_prewarm, "_PREWARM_THREAD", None)
    monkeypatch.setattr(runtime_prewarm, "_PREWARM_RUNNING", False)
    monkeypatch.setattr(runtime_prewarm, "_PREWARM_STOP", Event())
    assert runtime_prewarm.stop_runtime_read_models_prewarm(timeout_seconds=0) is True
    assert runtime_prewarm.prime_runtime_read_models_async() is False
    assert runtime_prewarm.resume_runtime_read_models_prewarm() is True
    assert runtime_prewarm._PREWARM_STOP.is_set() is False
