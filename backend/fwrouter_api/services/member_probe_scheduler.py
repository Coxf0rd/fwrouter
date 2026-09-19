from __future__ import annotations

from threading import Event, Lock, Thread

from fwrouter_api.core.config import get_settings
from fwrouter_api.services.logical_topology import probe_members
from fwrouter_api.services.logs import write_technical_log


_MEMBER_PROBE_THREAD: Thread | None = None
_MEMBER_PROBE_STOP_EVENT = Event()
_MEMBER_PROBE_LOCK = Lock()


def _member_probe_scheduler_loop() -> None:
    settings = get_settings()
    while not _MEMBER_PROBE_STOP_EVENT.is_set():
        try:
            probe_members(
                budget=settings.member_probe_budget,
                timeout_ms=settings.member_probe_timeout_ms,
            )
        except Exception as exc:
            write_technical_log(
                component="member-probe-scheduler",
                level="warning",
                event_type="member_probe_scheduler_failed",
                message="Logical member probe scheduler tick failed.",
                details={"error": str(exc)},
            )
        if _MEMBER_PROBE_STOP_EVENT.wait(settings.member_probe_interval_seconds):
            break


def start_member_probe_scheduler() -> bool:
    settings = get_settings()
    if not settings.member_probe_scheduler_enabled:
        return False
    global _MEMBER_PROBE_THREAD
    with _MEMBER_PROBE_LOCK:
        if _MEMBER_PROBE_THREAD is not None and _MEMBER_PROBE_THREAD.is_alive():
            return False
        _MEMBER_PROBE_STOP_EVENT.clear()
        _MEMBER_PROBE_THREAD = Thread(
            target=_member_probe_scheduler_loop,
            name="fwrouter-member-probes",
            daemon=True,
        )
        _MEMBER_PROBE_THREAD.start()
        return True


def stop_member_probe_scheduler(*, timeout_seconds: float = 2.0) -> bool:
    global _MEMBER_PROBE_THREAD
    with _MEMBER_PROBE_LOCK:
        if _MEMBER_PROBE_THREAD is None:
            return False
        _MEMBER_PROBE_STOP_EVENT.set()
        _MEMBER_PROBE_THREAD.join(timeout=timeout_seconds)
        stopped = not _MEMBER_PROBE_THREAD.is_alive()
        _MEMBER_PROBE_THREAD = None
        return stopped
