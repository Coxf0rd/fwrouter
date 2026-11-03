from __future__ import annotations

from threading import Thread

from fwrouter_api.core.config import get_settings
from fwrouter_api.jobs.manager import get_default_job_manager
from fwrouter_api.services.logs import write_technical_log
from fwrouter_api.services.worker_lifecycle import WorkerLifecycle


_WORKER = WorkerLifecycle()


def _run_subject_inventory_job() -> None:
    manager = get_default_job_manager()
    job = manager.create(
        "subject_inventory_sync",
        lock_key="subject_inventory_sync",
        requested_by="subject-inventory-scheduler",
        input_data={
            "discover_docker": True,
            "discover_host": True,
            "discover_xray": False,
            "discover_external_ingress_providers": [],
        },
    )
    manager.start_job_and_wait(job["job_id"])


def _subject_inventory_scheduler_loop() -> None:
    settings = get_settings()
    startup_delay = settings.subject_inventory_startup_delay_seconds
    interval = settings.subject_inventory_interval_seconds

    if startup_delay and _WORKER.stop_event.wait(startup_delay):
        return

    while not _WORKER.stop_event.is_set():
        try:
            _run_subject_inventory_job()
        except Exception as exc:
            write_technical_log(
                component="subject-inventory-scheduler",
                level="warning",
                event_type="subject_inventory_scheduler_failed",
                message="Subject inventory scheduler tick failed.",
                details={"error": str(exc)},
            )

        if _WORKER.stop_event.wait(interval):
            break


def start_subject_inventory_scheduler() -> bool:
    settings = get_settings()
    if not settings.subject_inventory_scheduler_enabled:
        return False

    return _WORKER.start(
        _subject_inventory_scheduler_loop,
        name="fwrouter-subject-inventory",
        thread_factory=Thread,
    )


def stop_subject_inventory_scheduler(*, timeout_seconds: float = 2.0) -> bool:
    return _WORKER.stop(timeout_seconds=timeout_seconds)
