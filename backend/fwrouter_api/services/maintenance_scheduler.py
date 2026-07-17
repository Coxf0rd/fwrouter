from __future__ import annotations

from threading import Event, Lock, Thread

from fwrouter_api.core.config import get_settings
from fwrouter_api.services.logs import write_technical_log
from fwrouter_api.services.maintenance import run_control_plane_maintenance


_MAINTENANCE_THREAD: Thread | None = None
_MAINTENANCE_STOP_EVENT = Event()
_MAINTENANCE_LOCK = Lock()


def _maintenance_scheduler_loop() -> None:
    settings = get_settings()
    interval = settings.maintenance_interval_seconds

    while not _MAINTENANCE_STOP_EVENT.is_set():
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

        if _MAINTENANCE_STOP_EVENT.wait(interval):
            break


def start_maintenance_scheduler() -> bool:
    settings = get_settings()
    if not settings.maintenance_scheduler_enabled:
        return False

    global _MAINTENANCE_THREAD
    with _MAINTENANCE_LOCK:
        if _MAINTENANCE_THREAD is not None and _MAINTENANCE_THREAD.is_alive():
            return False

        _MAINTENANCE_STOP_EVENT.clear()
        _MAINTENANCE_THREAD = Thread(
            target=_maintenance_scheduler_loop,
            name="fwrouter-maintenance",
            daemon=True,
        )
        _MAINTENANCE_THREAD.start()
        return True


def stop_maintenance_scheduler(*, timeout_seconds: float = 2.0) -> bool:
    global _MAINTENANCE_THREAD
    with _MAINTENANCE_LOCK:
        if _MAINTENANCE_THREAD is None:
            return False

        _MAINTENANCE_STOP_EVENT.set()
        _MAINTENANCE_THREAD.join(timeout=timeout_seconds)
        stopped = not _MAINTENANCE_THREAD.is_alive()
        _MAINTENANCE_THREAD = None
        return stopped
