from __future__ import annotations

from threading import Event, Lock, Thread

from fwrouter_api.core.config import get_settings
from fwrouter_api.jobs.manager import get_default_job_manager
from fwrouter_api.services.logs import write_technical_log


_SUBJECT_INVENTORY_THREAD: Thread | None = None
_SUBJECT_INVENTORY_STOP_EVENT = Event()
_SUBJECT_INVENTORY_LOCK = Lock()


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

    if startup_delay and _SUBJECT_INVENTORY_STOP_EVENT.wait(startup_delay):
        return

    while not _SUBJECT_INVENTORY_STOP_EVENT.is_set():
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

        if _SUBJECT_INVENTORY_STOP_EVENT.wait(interval):
            break


def start_subject_inventory_scheduler() -> bool:
    settings = get_settings()
    if not settings.subject_inventory_scheduler_enabled:
        return False

    global _SUBJECT_INVENTORY_THREAD
    with _SUBJECT_INVENTORY_LOCK:
        if _SUBJECT_INVENTORY_THREAD is not None and _SUBJECT_INVENTORY_THREAD.is_alive():
            return False

        _SUBJECT_INVENTORY_STOP_EVENT.clear()
        _SUBJECT_INVENTORY_THREAD = Thread(
            target=_subject_inventory_scheduler_loop,
            name="fwrouter-subject-inventory",
            daemon=True,
        )
        _SUBJECT_INVENTORY_THREAD.start()
        return True


def stop_subject_inventory_scheduler(*, timeout_seconds: float = 2.0) -> bool:
    global _SUBJECT_INVENTORY_THREAD
    with _SUBJECT_INVENTORY_LOCK:
        if _SUBJECT_INVENTORY_THREAD is None:
            return False

        _SUBJECT_INVENTORY_STOP_EVENT.set()
        _SUBJECT_INVENTORY_THREAD.join(timeout=timeout_seconds)
        stopped = not _SUBJECT_INVENTORY_THREAD.is_alive()
        _SUBJECT_INVENTORY_THREAD = None
        return stopped
