from __future__ import annotations

from threading import Thread

from fwrouter_api.core.config import get_settings
from fwrouter_api.services.logs import write_technical_log
from fwrouter_api.services.runtime_convergence import run_runtime_convergence_check
from fwrouter_api.services.xray_vpn_auto_pending import dispatch_due_xray_vpn_auto_reconcile
from fwrouter_api.services.worker_lifecycle import WorkerLifecycle


_WORKER = WorkerLifecycle()


def _runtime_convergence_scheduler_loop() -> None:
    settings = get_settings()
    interval = settings.runtime_convergence_interval_seconds

    while not _WORKER.stop_event.is_set():
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

        try:
            dispatch_due_xray_vpn_auto_reconcile()
        except Exception as exc:
            write_technical_log(
                component="runtime-convergence-scheduler",
                level="warning",
                event_type="xray_vpn_auto_reconcile_dispatch_failed",
                message="Xray vpn-auto pending reconcile dispatch failed.",
                details={"error": str(exc)},
            )

        if _WORKER.stop_event.wait(interval):
            break


def start_runtime_convergence_scheduler() -> bool:
    settings = get_settings()
    if not settings.runtime_convergence_scheduler_enabled:
        return False

    return _WORKER.start(
        _runtime_convergence_scheduler_loop,
        name="fwrouter-runtime-convergence",
        thread_factory=Thread,
    )


def stop_runtime_convergence_scheduler(*, timeout_seconds: float = 2.0) -> bool:
    return _WORKER.stop(timeout_seconds=timeout_seconds)
