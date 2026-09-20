from __future__ import annotations

from threading import Event, Lock, Thread

from fwrouter_api.core.config import get_settings
from fwrouter_api.services.logical_topology import observe_active_paths
from fwrouter_api.services.logs import write_technical_log


_THREAD: Thread | None = None
_STOP = Event()
_LOCK = Lock()


def _loop() -> None:
    settings = get_settings()
    while not _STOP.is_set():
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
        if _STOP.wait(settings.active_observation_interval_seconds):
            break


def start_active_observation_scheduler() -> bool:
    settings = get_settings()
    if not settings.active_observation_scheduler_enabled:
        return False
    global _THREAD
    with _LOCK:
        if _THREAD is not None and _THREAD.is_alive():
            return False
        _STOP.clear()
        _THREAD = Thread(target=_loop, name="fwrouter-active-observation", daemon=True)
        _THREAD.start()
        return True


def stop_active_observation_scheduler(*, timeout_seconds: float = 2.0) -> bool:
    global _THREAD
    with _LOCK:
        if _THREAD is None:
            return False
        _STOP.set()
        _THREAD.join(timeout=timeout_seconds)
        stopped = not _THREAD.is_alive()
        _THREAD = None
        return stopped
