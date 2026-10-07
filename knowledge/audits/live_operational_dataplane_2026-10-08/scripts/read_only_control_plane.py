#!/usr/bin/env python3
"""Bounded read-only FWRouter API latency cohort. Never stores response bodies."""
from __future__ import annotations
import json
import os
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

BASE = os.environ.get("FWROUTER_BENCH_BASE", "http://127.0.0.1:5000/api/v2")
N = int(os.environ.get("FWROUTER_BENCH_N", "5"))
CGROUP = Path("/sys/fs/cgroup/system.slice/fwrouter-api.service")
PATHS = {
    "health": "/health",
    "summary": "/system/summary",
    "selector_state": "/selector/vpn-auto/state",
    "jobs": "/jobs?limit=5",
    "events": "/events/recent?limit=5&view=summary",
    "settings_workspace": "/ui/settings/workspace",
}

def snapshot():
    result = {}
    try:
        result["cgroup_cpu_usec"] = int(next(x.split()[1] for x in (CGROUP / "cpu.stat").read_text().splitlines() if x.startswith("usage_usec ")))
        result["cgroup_memory_current"] = int((CGROUP / "memory.current").read_text().strip())
    except (OSError, StopIteration, ValueError):
        result["cgroup_unavailable"] = True
    return result

def empty_observer_calibration(samples=60):
    durations = []
    for _ in range(samples):
        start = time.perf_counter_ns()
        snapshot()
        durations.append(time.perf_counter_ns() - start)
    return {"samples": samples, "snapshot_ns_p50": sorted(durations)[samples // 2], "snapshot_ns_max": max(durations)}

def read_one(path):
    request = urllib.request.Request(BASE + path, headers={"Accept": "application/json", "Cache-Control": "no-cache"}, method="GET")
    start = time.perf_counter_ns()
    status = None
    size = 0
    error_type = None
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            status = response.status
            while True:
                chunk = response.read(65536)
                if not chunk:
                    break
                size += len(chunk)
    except urllib.error.HTTPError as exc:
        status = exc.code
        while True:
            chunk = exc.read(65536)
            if not chunk:
                break
            size += len(chunk)
    except Exception as exc:
        error_type = type(exc).__name__
    elapsed = time.perf_counter_ns() - start
    return {"status": status, "response_bytes": size, "latency_ms": round(elapsed / 1e6, 3), "error_type": error_type}

def main():
    result = {
        "captured_at_utc": datetime.now(timezone.utc).isoformat(),
        "source_baseline": "6ba7f6f",
        "classification": "measured_live_read_only",
        "scope": "serial API GETs; body bytes counted and discarded; no secret payload written",
        "n_per_endpoint": N,
        "observer_calibration": empty_observer_calibration(),
        "endpoints": {},
    }
    before = snapshot()
    for name, path in PATHS.items():
        rows = [read_one(path) for _ in range(N)]
        latency = sorted(row["latency_ms"] for row in rows if row["latency_ms"] is not None)
        result["endpoints"][name] = {
            "path": path,
            "samples": rows,
            "latency_ms": {
                "p50": latency[(len(latency) - 1) // 2] if latency else None,
                "p95_nearest_rank": latency[max(0, (95 * len(latency) + 99) // 100 - 1)] if latency else None,
                "p99_nearest_rank": latency[max(0, (99 * len(latency) + 99) // 100 - 1)] if latency else None,
            },
        }
    after = snapshot()
    result["resource_window"] = {"before": before, "after": after}
    if "cgroup_cpu_usec" in before and "cgroup_cpu_usec" in after:
        result["resource_window"]["cgroup_cpu_delta_usec"] = after["cgroup_cpu_usec"] - before["cgroup_cpu_usec"]
    print(json.dumps(result, ensure_ascii=False, indent=2))

if __name__ == "__main__":
    main()
