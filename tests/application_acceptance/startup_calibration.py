"""Bounded synthetic CPU calibration outside the acceptance readiness window."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import stat
import time
from typing import Any


def cpu_calibration() -> dict[str, Any]:
    """Three identical workloads with a two-second cooperative budget."""
    overall = time.monotonic()
    payload = json.dumps([{"i": index, "v": "x" * 96} for index in range(8192)],
                         separators=(",", ":"), sort_keys=True)
    encoded = payload.encode("ascii")
    fingerprint = hashlib.sha256(encoded).hexdigest()
    samples: list[dict[str, Any]] = []
    for _ in range(3):
        started = time.monotonic()
        cpu = time.process_time()
        thread_cpu = time.thread_time()
        completed = 0
        for _ in range(8):
            if time.monotonic() - overall >= 2.0:
                break
            decoded = json.loads(payload)
            checksum = hashlib.sha256(encoded).hexdigest()
            if len(decoded) != 8192 or checksum != fingerprint:
                raise AssertionError("synthetic calibration workload changed")
            completed += 1
        samples.append({"iterations_completed": completed,
                        "wall_seconds": round(time.monotonic() - started, 6),
                        "process_cpu_seconds": round(time.process_time() - cpu, 6),
                        "thread_cpu_seconds": round(time.thread_time() - thread_cpu, 6)})
        if completed != 8:
            break
    return {"schema": "fwrouter-startup-cpu-calibration/v1",
            "status": "complete" if len(samples) == 3 and all(x["iterations_completed"] == 8 for x in samples) else "incomplete",
            "scope": "pytest_process_before_readiness_window_not_worker",
            "input_bytes": len(encoded), "objects_per_decode": 8192,
            "workload_sha256": fingerprint, "samples_required": 3,
            "iterations_per_sample": 8, "budget_seconds": 2.0,
            "budget_semantics": "checked_between_fixed_work_units_not_a_preemptive_timeout",
            "samples": samples,
            "total_wall_seconds": round(time.monotonic() - overall, 6)}


def read_startup_counters(worker_pid: int) -> dict[str, Any]:
    """Read the test-only worker snapshot, never a prior fixture's worker."""
    path = Path("/tmp/fwrouter-receipts/startup-causality-worker.json")
    try:
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_size > 512 * 1024:
            return {"status": "unavailable", "reason": "snapshot_not_owned_bounded_regular_file"}
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(descriptor, "rb") as stream:
            actual = os.fstat(stream.fileno())
            if not stat.S_ISREG(actual.st_mode) or actual.st_uid != os.getuid():
                return {"status": "unavailable", "reason": "snapshot_descriptor_not_owned_regular_file"}
            raw = stream.read(512 * 1024 + 1)
        if len(raw) > 512 * 1024:
            return {"status": "unavailable", "reason": "snapshot_too_large"}
        report = json.loads(raw)
        if not isinstance(report, dict) or report.get("schema") != "fwrouter-startup-causality/v1" or report.get("worker_pid") != worker_pid:
            return {"status": "unavailable", "reason": "snapshot_worker_or_schema_mismatch"}
        return {"status": "available", "report": report}
    except (OSError, ValueError, UnicodeError) as exc:
        return {"status": "unavailable", "reason": type(exc).__name__}
