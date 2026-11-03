from __future__ import annotations

from threading import Thread

from fwrouter_api.core.config import get_settings
from fwrouter_api.services.logical_topology import observe_active_paths
from fwrouter_api.services.logs import write_technical_log
from fwrouter_api.services.worker_lifecycle import WorkerLifecycle


_WORKER = WorkerLifecycle()


def _loop() -> None:
    settings = get_settings()
    while not _WORKER.stop_event.is_set():
        try:
            observe_active_paths()
        except Exception as exc:
            write_technical_log(
                component="active-observation-scheduler",
                level="warning",
                event_type="active_observation_scheduler_failed",
                message="Active runtime observation tick failed.",
                details={"error": str(exc)},
            )
        if _WORKER.stop_event.wait(settings.active_observation_interval_seconds):
            break


def start_active_observation_scheduler() -> bool:
    settings = get_settings()
    if not settings.active_observation_scheduler_enabled:
        return False
    return _WORKER.start(
        _loop,
        name="fwrouter-active-observation",
        thread_factory=Thread,
    )


def stop_active_observation_scheduler(*, timeout_seconds: float = 2.0) -> bool:
    return _WORKER.stop(timeout_seconds=timeout_seconds)
