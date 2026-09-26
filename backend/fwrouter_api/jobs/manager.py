"""SQLite-backed job runner / manager for FWRouter v2."""

from __future__ import annotations

from collections.abc import Callable
from threading import Lock, Thread
import time
from typing import Any

from fwrouter_api.core.config import get_settings
from fwrouter_api.services.logs import write_technical_log
from fwrouter_api.services.event_contract import (
    current_event_context,
    reset_event_context,
    set_event_context,
)
from fwrouter_api.services.jobs import (
    cleanup_stale_running_jobs,
    create_job,
    get_job,
    list_jobs,
    mark_job_failed,
    mark_job_running,
    mark_job_success,
)


JobHandler = Callable[[dict[str, Any]], dict[str, Any] | None]


class JobManager:
    """Small SQLite-backed manager for backend jobs.

    This manager does not execute arbitrary commands and does not know how to
    apply runtime changes by itself. Real behavior is added only through explicit
    registered handlers.
    """

    def __init__(self) -> None:
        self._handlers: dict[str, JobHandler] = {}
        self._lock = Lock()
        self._workers: dict[str, Thread] = {}

    def register_handler(self, job_type: str, handler: JobHandler) -> None:
        """Register a handler for one job type."""

        with self._lock:
            self._handlers[job_type] = handler

    def create(
        self,
        job_type: str,
        *,
        lock_key: str | None = None,
        requested_by: str | None = None,
        input_data: dict[str, Any] | None = None,
        artifact_dir: str | None = None,
    ) -> dict[str, Any]:
        """Create a queued job in SQLite."""

        self.cleanup_stale_jobs()
        return create_job(
            job_type,
            lock_key=lock_key,
            requested_by=requested_by,
            input_data=input_data,
            artifact_dir=artifact_dir,
            event_context=current_event_context(),
        )

    def get_job(self, job_id: str) -> dict[str, Any] | None:
        """Get one job by ID."""

        self.cleanup_stale_jobs()
        return get_job(job_id)

    def list_jobs(
        self,
        *,
        limit: int = 50,
        job_type: str | None = None,
        status: str | None = None,
    ) -> list[dict[str, Any]]:
        """Return recent jobs from SQLite."""

        self.cleanup_stale_jobs()
        return list_jobs(limit=limit, job_type=job_type, status=status)

    def cleanup_stale_jobs(self) -> list[dict[str, Any]]:
        """Mark stale running jobs failed before they block new work."""

        return cleanup_stale_running_jobs(
            stale_after_seconds=int(get_settings().job_stale_timeout_seconds)
        )

    def _get_handler(self, job_type: str) -> JobHandler | None:
        with self._lock:
            return self._handlers.get(job_type)

    def _prune_workers(self) -> None:
        with self._lock:
            stale_ids = [job_id for job_id, worker in self._workers.items() if not worker.is_alive()]
            for job_id in stale_ids:
                self._workers.pop(job_id, None)

    def start_job(self, job_id: str) -> dict[str, Any] | None:
        """Start one job in a background thread and return current state."""

        self.cleanup_stale_jobs()
        job = get_job(job_id)
        if job is None:
            return None

        if job["status"] in {"success", "failed", "cancelled"}:
            return job

        self._prune_workers()
        with self._lock:
            worker = self._workers.get(job_id)
            if worker is not None and worker.is_alive():
                return get_job(job_id)

            worker = Thread(
                target=self._run_job_worker,
                args=(job_id,),
                name=f"fwrouter-job-{job_id}",
                daemon=True,
            )
            self._workers[job_id] = worker
            worker.start()

        deadline = time.monotonic() + 0.5
        while time.monotonic() < deadline:
            current = get_job(job_id)
            if current is None:
                return None
            if current["status"] != "queued":
                return current
            time.sleep(0.02)

        return get_job(job_id)

    def wait_for_job(
        self,
        job_id: str,
        *,
        timeout_seconds: int | None = None,
        poll_interval_seconds: float = 0.2,
    ) -> dict[str, Any] | None:
        """Wait bounded time for job completion, then return current job state."""

        wait_timeout = int(
            timeout_seconds
            if timeout_seconds is not None
            else get_settings().job_run_now_wait_timeout_seconds
        )
        deadline = time.monotonic() + max(wait_timeout, 0)

        while True:
            self.cleanup_stale_jobs()
            job = get_job(job_id)
            if job is None:
                return None
            if job["status"] in {"success", "failed", "cancelled"}:
                return job
            if time.monotonic() >= deadline:
                return job
            time.sleep(max(poll_interval_seconds, 0.05))

    def wait_for_idle(self, *, timeout_seconds: float = 5.0) -> bool:
        """Wait for already-started worker threads to finish."""

        deadline = time.monotonic() + max(timeout_seconds, 0.0)
        while True:
            with self._lock:
                workers = list(self._workers.values())

            live_workers = [worker for worker in workers if worker.is_alive()]
            if not live_workers:
                self._prune_workers()
                return True

            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False

            for worker in live_workers:
                worker.join(timeout=min(0.1, max(remaining, 0.0)))

    def start_job_and_wait(
        self,
        job_id: str,
        *,
        timeout_seconds: int | None = None,
    ) -> dict[str, Any] | None:
        """Launch job asynchronously and wait only for a bounded window."""

        started = self.start_job(job_id)
        if started is None:
            return None
        if started["status"] in {"success", "failed", "cancelled"}:
            return started
        return self.wait_for_job(job_id, timeout_seconds=timeout_seconds)

    def _run_job_worker(self, job_id: str) -> None:
        try:
            self.run_job(job_id)
        finally:
            self._prune_workers()

    def run_job(self, job_id: str) -> dict[str, Any] | None:
        """Run a registered handler for one queued job."""

        self.cleanup_stale_jobs()
        job = mark_job_running(job_id)
        if job is None:
            return None

        if job["status"] != "running":
            return job

        handler = self._get_handler(job["job_type"])

        if handler is None:
            return mark_job_failed(
                job_id,
                error_code="JOB_HANDLER_NOT_REGISTERED",
                error_message=f"No handler registered for job type: {job['job_type']}",
            )

        inherited_context = job.get("event_context") if isinstance(job.get("event_context"), dict) else {}
        context_token = set_event_context(
            **inherited_context,
            job_id=job_id,
            workflow_id=inherited_context.get("workflow_id") or job_id,
            causation_id=inherited_context.get("causation_id") or inherited_context.get("request_id"),
        )
        try:
            result = handler(job) or {}
        except Exception as exc:
            write_technical_log(
                component="jobs-manager",
                level="warning",
                event_type="job_handler_exception",
                message="Job handler raised an exception.",
                details={
                    "job_id": job_id,
                    "job_type": job["job_type"],
                    "lock_key": job.get("lock_key"),
                    "error": str(exc),
                },
            )
            return mark_job_failed(
                job_id,
                error_code="JOB_HANDLER_FAILED",
                error_message=str(exc),
            )
        finally:
            reset_event_context(context_token)

        if result.get("job_status") == "failed":
            return mark_job_failed(
                job_id,
                error_code=str(result.get("error_code") or "JOB_HANDLER_FAILED"),
                error_message=str(result.get("error_message") or "Job handler reported failure."),
                result=result,
            )

        return mark_job_success(job_id, result=result)


DEFAULT_JOB_MANAGER = JobManager()
_default_handlers_registered = False


def get_default_job_manager() -> JobManager:
    """Return global job manager with built-in safe handlers registered once."""

    global _default_handlers_registered

    if not _default_handlers_registered:
        from fwrouter_api.jobs.handlers import register_default_handlers

        register_default_handlers(DEFAULT_JOB_MANAGER)
        _default_handlers_registered = True

    return DEFAULT_JOB_MANAGER
