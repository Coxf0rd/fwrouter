from __future__ import annotations

import resource
import sys
import time
from typing import Any

from fwrouter_api.core.config import get_settings
from fwrouter_api.services.apply_plan import ApplyJobAbortedError
from fwrouter_api.services.jobs import (
    get_job_without_cleanup,
    touch_job_running,
    update_job_running_result as _update_job_running_result,
)
from fwrouter_api.services.artifacts import write_job_json_artifact


class ApplyPhaseTracker:
    """Record apply lifecycle phases and refresh the running job lease."""

    def __init__(self, *, job_id: str, apply_id: str) -> None:
        self.job_id = job_id
        self.apply_id = apply_id
        self.timeout_seconds = int(get_settings().apply_phase_timeout_seconds)
        self.events: list[dict[str, Any]] = []
        self.current_phase: str | None = None
        self.current_started_at: float | None = None
        self._write()

    def begin(self, phase: str, **details: Any) -> None:
        self.current_phase = phase
        self.current_started_at = time.monotonic()
        touch_job_running(self.job_id)
        self.events.append(
            {
                "phase": phase,
                "event": "start",
                "ts": time.time(),
                "details": details,
            }
        )
        self._write()

    def finish(self, **details: Any) -> None:
        from fwrouter_api.services.apply_plan import ApplyPhaseTimeoutError

        phase = self.current_phase or "unknown"
        duration = None
        if self.current_started_at is not None:
            duration = time.monotonic() - self.current_started_at
        touch_job_running(self.job_id)
        self.events.append(
            {
                "phase": phase,
                "event": "finish",
                "ts": time.time(),
                "duration_seconds": duration,
                "details": details,
            }
        )
        self._write()
        self.current_phase = None
        self.current_started_at = None
        if duration is not None and duration > self.timeout_seconds:
            raise ApplyPhaseTimeoutError(
                f"Apply phase exceeded timeout: {phase} took {duration:.1f}s > {self.timeout_seconds}s."
            )

    def _write(self) -> None:
        facade = sys.modules.get("fwrouter_api.services.apply")
        update_job_running_result = getattr(
            facade,
            "update_job_running_result",
            _update_job_running_result,
        )
        snapshot = {
            "apply_id": self.apply_id,
            "current_phase": self.current_phase,
            "events": self.events,
        }
        write_job_json_artifact(
            self.job_id,
            "dataplane/phases.json",
            snapshot,
        )
        update_job_running_result(
            self.job_id,
            result={
                "job_status": "running",
                "stage": self.current_phase or "queued",
                "apply": snapshot,
            },
        )


def memory_snapshot() -> dict[str, Any]:
    usage = resource.getrusage(resource.RUSAGE_SELF)
    return {
        "rss_kb": int(usage.ru_maxrss),
        "user_cpu_seconds": round(float(usage.ru_utime), 3),
        "system_cpu_seconds": round(float(usage.ru_stime), 3),
    }


def require_job_running(job_id: str, *, phase: str) -> None:
    job = get_job_without_cleanup(job_id)
    if job is None or job.get("status") not in {"queued", "running"}:
        raise ApplyJobAbortedError(
            f"Apply job is no longer active during phase {phase}."
        )
