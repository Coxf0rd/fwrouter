"""Application lifespan coordination over existing process-owned services.

This coordinates the one supported native API process lifetime. Injected
operations make lifecycle behavior testable; production operations still point
at the existing process singletons and do not create an isolated runtime.
"""
from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from time import monotonic
from typing import Any


SHUTDOWN_BUDGET_SECONDS = 14.0


@dataclass(frozen=True)
class ApplicationLifecycleOperations:
    """Narrow operations used by the application lifespan coordinator."""

    bootstrap: Callable[[], dict[str, Any]]
    settings_provider: Callable[[], Any]
    scrub_logs: Callable[[list[Any]], Any]
    job_manager_provider: Callable[[], Any]
    register_extended_handlers: Callable[[Any], Any]
    start_schedulers: tuple[Callable[[], Any], ...]
    stop_schedulers: tuple[Callable[..., Any], ...]
    resume_prewarm: Callable[[], bool]
    prime_prewarm: Callable[..., Any]
    stop_prewarm: Callable[..., Any]
    monotonic: Callable[[], float] = monotonic


class ApplicationLifecycle:
    """Own ordered startup and bounded cooperative shutdown coordination."""

    def __init__(self, operations: ApplicationLifecycleOperations) -> None:
        self._operations = operations

    @asynccontextmanager
    async def run(self, *, startup_tasks_enabled: bool) -> AsyncIterator[None]:
        operations = self._operations
        job_manager = None
        try:
            if startup_tasks_enabled:
                bootstrap_result = operations.bootstrap()
                if not bootstrap_result.get("database_schema", {}).get("ok"):
                    # No log cleanup, handler registration or worker startup may
                    # follow failed full database admission.
                    raise RuntimeError("FWROUTER_DATABASE_ADMISSION_FAILED")

                paths = operations.settings_provider().paths
                operations.scrub_logs([paths.operational_log_dir, paths.technical_log_dir])
                job_manager = operations.job_manager_provider()
                operations.register_extended_handlers(job_manager)
                for start in operations.start_schedulers:
                    start()
                if operations.resume_prewarm():
                    operations.prime_prewarm(include_global_profiles=False)

            yield
        finally:
            # Signal all schedulers before joining any of them so shutdown
            # shares one budget instead of paying one timeout per worker.
            scheduler_stops = operations.stop_schedulers if startup_tasks_enabled else ()
            for stop in scheduler_stops:
                stop(timeout_seconds=0.0)
            operations.stop_prewarm(timeout_seconds=0.0)

            deadline = operations.monotonic() + SHUTDOWN_BUDGET_SECONDS
            for stop in scheduler_stops:
                stop(timeout_seconds=max(0.0, deadline - operations.monotonic()))
            operations.stop_prewarm(
                timeout_seconds=max(0.0, deadline - operations.monotonic()),
            )
            if job_manager is None:
                job_manager = operations.job_manager_provider()
            job_manager.wait_for_idle(
                timeout_seconds=max(0.0, deadline - operations.monotonic()),
            )
