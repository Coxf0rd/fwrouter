from __future__ import annotations

from threading import Thread

from fwrouter_api.core.config import get_settings
from fwrouter_api.services.logs import write_technical_log
from fwrouter_api.services.maintenance import run_control_plane_maintenance
from fwrouter_api.services.worker_lifecycle import WorkerLifecycle


_WORKER = WorkerLifecycle()


def _maintenance_scheduler_loop() -> None:
    settings = get_settings()
    interval = settings.maintenance_interval_seconds

    while not _WORKER.stop_event.is_set():
        try:
            run_control_plane_maintenance(dry_run=False)
        except Exception as exc:
            write_technical_log(
                component="maintenance-scheduler",
                level="warning",
                event_type="maintenance_scheduler_failed",
                message="Control-plane maintenance scheduler tick failed.",
                details={"error": str(exc)},
            )

        if _WORKER.stop_event.wait(interval):
            break


def start_maintenance_scheduler() -> bool:
    settings = get_settings()
    if not settings.maintenance_scheduler_enabled:
        return False

    return _WORKER.start(
        _maintenance_scheduler_loop,
        name="fwrouter-maintenance",
        thread_factory=Thread,
    )


def stop_maintenance_scheduler(*, timeout_seconds: float = 2.0) -> bool:
    return _WORKER.stop(timeout_seconds=timeout_seconds)
