from __future__ import annotations

from threading import Barrier, Event, Thread
from types import SimpleNamespace
from concurrent.futures import ThreadPoolExecutor

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
    ("module", "worker_name", "loop_name", "enabled_setting", "start", "stop"),
    [
        (
            maintenance_scheduler,
            "_WORKER",
            "_maintenance_scheduler_loop",
            "maintenance_scheduler_enabled",
            maintenance_scheduler.start_maintenance_scheduler,
            maintenance_scheduler.stop_maintenance_scheduler,
        ),
        (
            member_probe_scheduler,
            "_WORKER",
            "_member_probe_scheduler_loop",
            "member_probe_scheduler_enabled",
            member_probe_scheduler.start_member_probe_scheduler,
            member_probe_scheduler.stop_member_probe_scheduler,
        ),
        (
            active_observation_scheduler,
            "_WORKER",
            "_loop",
            "active_observation_scheduler_enabled",
            active_observation_scheduler.start_active_observation_scheduler,
            active_observation_scheduler.stop_active_observation_scheduler,
        ),
        (
            subject_inventory_scheduler,
            "_WORKER",
            "_subject_inventory_scheduler_loop",
            "subject_inventory_scheduler_enabled",
            subject_inventory_scheduler.start_subject_inventory_scheduler,
            subject_inventory_scheduler.stop_subject_inventory_scheduler,
        ),
        (
            external_collectors,
            "_SCHEDULER_WORKER",
            "_external_collector_scheduler_loop",
            "external_collector_scheduler_enabled",
            external_collectors.start_external_collector_scheduler,
            external_collectors.stop_external_collector_scheduler,
        ),
        (
            runtime_convergence_scheduler,
            "_WORKER",
            "_runtime_convergence_scheduler_loop",
            "runtime_convergence_scheduler_enabled",
            runtime_convergence_scheduler.start_runtime_convergence_scheduler,
            runtime_convergence_scheduler.stop_runtime_convergence_scheduler,
        ),
        (
            watchdog_scheduler,
            "_WORKER",
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
    worker_name: str,
    loop_name: str,
    enabled_setting: str,
    start,
    stop,
) -> None:
    worker = getattr(module, worker_name)
    existing = worker.thread
    assert existing is None or not existing.is_alive()
    entered = Event()
    release = Event()
    second_entered = Event()
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
    first_worker = worker.thread

    try:
        assert stop(timeout_seconds=0) is False
        assert worker.thread is first_worker
        assert start() is False
        assert launches == 1
        assert worker.stop_event.is_set()

        release.set()
        assert stop(timeout_seconds=2) is True
        assert worker.thread is None

        assert start() is True
        assert second_entered.wait(timeout=2)
        assert stop(timeout_seconds=2) is True
        assert worker.thread is None
    finally:
        release.set()
        current = worker.thread
        if current is not None:
            current.join(timeout=2)
            stop(timeout_seconds=2)


def test_worker_lifecycle_releases_unstarted_handle_after_factory_start_failure() -> None:
    from fwrouter_api.services.worker_lifecycle import WorkerLifecycle

    worker = WorkerLifecycle()

    class FailedStartThread(Thread):
        def start(self) -> None:
            raise RuntimeError("synthetic thread-start failure")

    def failed_factory(**kwargs) -> Thread:
        return FailedStartThread(**kwargs)

    with pytest.raises(RuntimeError, match="synthetic thread-start failure"):
        worker.start(lambda: None, name="failed-worker", thread_factory=failed_factory)
    assert worker.thread is None
    assert worker.stop() is False

    entered = Event()
    release = Event()

    def held_worker() -> None:
        entered.set()
        if not release.wait(timeout=5):
            return

    try:
        assert worker.start(held_worker, name="retry-worker") is True
        assert entered.wait(timeout=2)
        assert worker.stop(timeout_seconds=0) is False
        assert worker.thread is not None and worker.thread.is_alive()
        release.set()
        assert worker.stop(timeout_seconds=2) is True
    finally:
        release.set()
        worker.stop(timeout_seconds=2)


def test_worker_lifecycle_retains_worker_that_starts_before_factory_error() -> None:
    from fwrouter_api.services.worker_lifecycle import WorkerLifecycle

    worker = WorkerLifecycle()
    entered = Event()
    release = Event()

    def held_worker() -> None:
        entered.set()
        if not release.wait(timeout=5):
            return

    class StartThenRaiseThread(Thread):
        def start(self) -> None:
            super().start()
            raise RuntimeError("synthetic post-start failure")

    try:
        with pytest.raises(RuntimeError, match="synthetic post-start failure"):
            worker.start(
                held_worker,
                name="partial-start-worker",
                thread_factory=StartThenRaiseThread,
            )

        assert entered.wait(timeout=2)
        thread = worker.thread
        assert thread is not None and thread.is_alive()
        assert worker.stop_event.is_set()
        assert worker.start(lambda: None, name="duplicate-worker") is False
        release.set()
        assert worker.stop(timeout_seconds=2) is True
    finally:
        release.set()
        worker.stop(timeout_seconds=2)


def test_worker_lifecycle_serializes_concurrent_start_requests() -> None:
    from fwrouter_api.services.worker_lifecycle import WorkerLifecycle

    worker = WorkerLifecycle()
    entered = Event()
    release = Event()
    attempts = Barrier(9)
    launches = 0

    def held_worker() -> None:
        nonlocal launches
        launches += 1
        entered.set()
        if not release.wait(timeout=5):
            return

    def start_together() -> bool:
        attempts.wait(timeout=2)
        return worker.start(held_worker, name="concurrent-worker")

    try:
        with ThreadPoolExecutor(max_workers=8) as pool:
            starts = [pool.submit(start_together) for _ in range(8)]
            attempts.wait(timeout=2)
            results = [future.result(timeout=2) for future in starts]

        assert results.count(True) == 1
        assert launches == 1
        assert entered.wait(timeout=2)
        release.set()
        assert worker.stop(timeout_seconds=2) is True
    finally:
        release.set()
        worker.stop(timeout_seconds=2)
