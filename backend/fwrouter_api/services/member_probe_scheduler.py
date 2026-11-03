from __future__ import annotations

from threading import Thread

from fwrouter_api.core.config import get_settings
from fwrouter_api.services.logical_topology import probe_members
from fwrouter_api.services.logs import write_technical_log
from fwrouter_api.services.worker_lifecycle import WorkerLifecycle


_WORKER = WorkerLifecycle()


def _member_probe_scheduler_loop() -> None:
    settings = get_settings()
    while not _WORKER.stop_event.is_set():
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
        if _WORKER.stop_event.wait(settings.member_probe_interval_seconds):
            break


def start_member_probe_scheduler() -> bool:
    settings = get_settings()
    if not settings.member_probe_scheduler_enabled:
        return False
    return _WORKER.start(
        _member_probe_scheduler_loop,
        name="fwrouter-member-probes",
        thread_factory=Thread,
    )


def stop_member_probe_scheduler(*, timeout_seconds: float = 2.0) -> bool:
    return _WORKER.stop(timeout_seconds=timeout_seconds)
