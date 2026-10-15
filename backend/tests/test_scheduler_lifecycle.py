from __future__ import annotations

from types import SimpleNamespace

import pytest

from fwrouter_api.services import (
    active_observation_scheduler,
    external_collectors,
    maintenance_scheduler,
    member_probe_scheduler,
    runtime_convergence_scheduler,
    subject_inventory_scheduler,
    watchdog_scheduler,
)


def _start_watchdog() -> bool:
    return watchdog_scheduler.start_scheduler(tick=lambda: {}, disabled=lambda: None)


@pytest.mark.parametrize(
    ("module", "thread_name", "stop_event_name", "loop_name", "enabled_setting", "start", "stop"),
    [
        (
            maintenance_scheduler,
            "_MAINTENANCE_THREAD",
            "_MAINTENANCE_STOP_EVENT",
            "_maintenance_scheduler_loop",
            "maintenance_scheduler_enabled",
            maintenance_scheduler.start_maintenance_scheduler,
            maintenance_scheduler.stop_maintenance_scheduler,
        ),
        (
            member_probe_scheduler,
            "_MEMBER_PROBE_THREAD",
            "_MEMBER_PROBE_STOP_EVENT",
            "_member_probe_scheduler_loop",
            "member_probe_scheduler_enabled",
            member_probe_scheduler.start_member_probe_scheduler,
            member_probe_scheduler.stop_member_probe_scheduler,
        ),
        (
            active_observation_scheduler,
            "_THREAD",
            "_STOP",
            "_loop",
            "active_observation_scheduler_enabled",
            active_observation_scheduler.start_active_observation_scheduler,
            active_observation_scheduler.stop_active_observation_scheduler,
        ),
        (
            subject_inventory_scheduler,
            "_SUBJECT_INVENTORY_THREAD",
            "_SUBJECT_INVENTORY_STOP_EVENT",
            "_subject_inventory_scheduler_loop",
            "subject_inventory_scheduler_enabled",
            subject_inventory_scheduler.start_subject_inventory_scheduler,
            subject_inventory_scheduler.stop_subject_inventory_scheduler,
        ),
        (
            external_collectors,
            "_SCHEDULER_THREAD",
            "_SCHEDULER_STOP",
            "_external_collector_scheduler_loop",
            "external_collector_scheduler_enabled",
            external_collectors.start_external_collector_scheduler,
            external_collectors.stop_external_collector_scheduler,
        ),
        (
            runtime_convergence_scheduler,
            "_RUNTIME_CONVERGENCE_THREAD",
            "_RUNTIME_CONVERGENCE_STOP_EVENT",
            "_runtime_convergence_scheduler_loop",
            "runtime_convergence_scheduler_enabled",
            runtime_convergence_scheduler.start_runtime_convergence_scheduler,
            runtime_convergence_scheduler.stop_runtime_convergence_scheduler,
        ),
        (
            watchdog_scheduler,
            "_THREAD",
            "_STOP_EVENT",
            "scheduler_loop",
            "watchdog_scheduler_enabled",
            _start_watchdog,
            watchdog_scheduler.stop_scheduler,
        ),
    ],
)
def test_timed_out_scheduler_stop_retains_worker_until_it_exits(
    monkeypatch,
    module,
    thread_name: str,
    stop_event_name: str,
    loop_name: str,
    enabled_setting: str,
    start,
    stop,
) -> None:
    existing = getattr(module, thread_name)
    assert existing is None or not existing.is_alive()
    if existing is not None:
        setattr(module, thread_name, None)

    entered = module.threading.Event() if module is external_collectors else module.Event()
    release = module.threading.Event() if module is external_collectors else module.Event()
    second_entered = module.threading.Event() if module is external_collectors else module.Event()
    launches = 0

    def blocked_worker(*_args, **_kwargs) -> None:
        nonlocal launches
        launches += 1
        if launches == 1:
            entered.set()
            release.wait()
        else:
            second_entered.set()

    monkeypatch.setattr(
        module,
        "get_settings",
        lambda: SimpleNamespace(**{enabled_setting: True}),
    )
    monkeypatch.setattr(module, loop_name, blocked_worker)

    assert start() is True
    assert entered.wait(timeout=2)
    first_worker = getattr(module, thread_name)

    try:
        assert stop(timeout_seconds=0) is False
        assert getattr(module, thread_name) is first_worker
        assert start() is False
        assert launches == 1
        assert getattr(module, stop_event_name).is_set()

        release.set()
        assert stop(timeout_seconds=2) is True
        assert getattr(module, thread_name) is None

        assert start() is True
        assert second_entered.wait(timeout=2)
        assert stop(timeout_seconds=2) is True
        assert getattr(module, thread_name) is None
    finally:
        release.set()
        current = getattr(module, thread_name)
        if current is not None:
            current.join(timeout=2)
            stop(timeout_seconds=2)
