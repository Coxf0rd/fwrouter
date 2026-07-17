from __future__ import annotations

from threading import Event, Lock, Thread

from fwrouter_api.core.config import get_settings
from fwrouter_api.services.logs import write_technical_log
from fwrouter_api.services.runtime_convergence import run_runtime_convergence_check


_RUNTIME_CONVERGENCE_THREAD: Thread | None = None
_RUNTIME_CONVERGENCE_STOP_EVENT = Event()
_RUNTIME_CONVERGENCE_LOCK = Lock()


def _runtime_convergence_scheduler_loop() -> None:
    settings = get_settings()
    interval = settings.runtime_convergence_interval_seconds

    while not _RUNTIME_CONVERGENCE_STOP_EVENT.is_set():
        try:
            run_runtime_convergence_check(
                requested_by="runtime_convergence_scheduler",
                log_events=True,
            )
        except Exception as exc:
            write_technical_log(
                component="runtime-convergence-scheduler",
                level="warning",
                event_type="runtime_convergence_scheduler_failed",
                message="Runtime convergence scheduler tick failed.",
                details={"error": str(exc)},
            )

        if _RUNTIME_CONVERGENCE_STOP_EVENT.wait(interval):
            break


def start_runtime_convergence_scheduler() -> bool:
    settings = get_settings()
    if not settings.runtime_convergence_scheduler_enabled:
        return False

    global _RUNTIME_CONVERGENCE_THREAD
    with _RUNTIME_CONVERGENCE_LOCK:
        if _RUNTIME_CONVERGENCE_THREAD is not None and _RUNTIME_CONVERGENCE_THREAD.is_alive():
            return False

        _RUNTIME_CONVERGENCE_STOP_EVENT.clear()
        _RUNTIME_CONVERGENCE_THREAD = Thread(
            target=_runtime_convergence_scheduler_loop,
            name="fwrouter-runtime-convergence",
            daemon=True,
        )
        _RUNTIME_CONVERGENCE_THREAD.start()
        return True


def stop_runtime_convergence_scheduler(*, timeout_seconds: float = 2.0) -> bool:
    global _RUNTIME_CONVERGENCE_THREAD
    with _RUNTIME_CONVERGENCE_LOCK:
        if _RUNTIME_CONVERGENCE_THREAD is None:
            return False

        _RUNTIME_CONVERGENCE_STOP_EVENT.set()
        _RUNTIME_CONVERGENCE_THREAD.join(timeout=timeout_seconds)
        stopped = not _RUNTIME_CONVERGENCE_THREAD.is_alive()
        _RUNTIME_CONVERGENCE_THREAD = None
        return stopped
