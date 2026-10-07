#!/usr/bin/env python3
"""Matched isolated benchmark of the manager-level cleanup sweep removal."""
from __future__ import annotations

import json
import os
import resource
import tempfile
import time
from pathlib import Path

from fwrouter_api.core.config import Settings, get_settings
from fwrouter_api.core.paths import FWRouterPaths

ROOT = Path(tempfile.mkdtemp(prefix="fwrouter-pass1-jobs-owned-")).resolve()
os.environ["FWROUTER_STATE_DIR"] = str(ROOT / "state")
os.environ["FWROUTER_ENVIRONMENT"] = "test"
os.environ["FWROUTER_JOB_STALE_TIMEOUT_SECONDS"] = "300"
os.environ["PYTHONDONTWRITEBYTECODE"] = "1"
Settings.model_config["env_file"] = None
Settings.paths = property(lambda _self: FWRouterPaths(
    etc_dir=ROOT / "etc", state_dir=ROOT / "state", log_dir=ROOT / "logs", run_dir=ROOT / "run"
))
get_settings.cache_clear()

from fwrouter_api.db import connection as db_connection  # noqa: E402
from fwrouter_api.db.connection import initialize_database  # noqa: E402
from fwrouter_api.jobs import manager as manager_module  # noqa: E402
from fwrouter_api.jobs.manager import JobManager  # noqa: E402
from fwrouter_api.services import jobs as jobs_service  # noqa: E402

initialize_database()
trace: dict[str, int] = {}
connections = 0
cleanup_calls = 0
original_connect = db_connection.connect
original_cleanup = jobs_service.cleanup_stale_running_jobs


def kind(statement: str) -> str:
    token = statement.strip().split(None, 1)[0].split("(", 1)[0].upper()
    return token if token in {"SELECT", "INSERT", "UPDATE", "DELETE", "REPLACE", "BEGIN", "COMMIT", "ROLLBACK", "PRAGMA"} else "OTHER"


def traced_connect():
    global connections
    connection = original_connect()
    connections += 1
    connection.set_trace_callback(lambda statement: trace.__setitem__(kind(statement), trace.get(kind(statement), 0) + 1))
    return connection


def counted_cleanup(*, stale_after_seconds=None):
    global cleanup_calls
    cleanup_calls += 1
    return original_cleanup(stale_after_seconds=stale_after_seconds)


db_connection.connect = traced_connect
jobs_service.cleanup_stale_running_jobs = counted_cleanup
manager_module.cleanup_stale_running_jobs = counted_cleanup
manager = JobManager()
mode = os.environ.get("FWROUTER_PASS1_BENCH_MODE", "fixed")
if mode not in {"baseline", "fixed"}:
    raise SystemExit("FWROUTER_PASS1_BENCH_MODE must be baseline or fixed")

trace.clear()
connections = 0
cleanup_calls = 0
count = 100
ru_before = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
io_before = Path("/proc/self/io").read_text().splitlines()
io_before = {key: int(value.strip()) for key, value in (line.split(":", 1) for line in io_before) if key in {"rchar", "wchar", "read_bytes", "write_bytes", "syscr", "syscw"}}
try:
    rss_before_kib = int(Path("/proc/self/status").read_text().split("VmRSS:")[1].split()[0])
except (OSError, ValueError, IndexError):
    rss_before_kib = None
db_path = get_settings().paths.db_path
wal_path = Path(f"{db_path}-wal")
wal_peak = wal_path.stat().st_size if wal_path.exists() else 0
wall_start = time.perf_counter()
cpu_start = time.process_time()
for _ in range(count):
    if mode == "baseline":
        # Reproduce the removed JobManager.create() pre-sweep.
        manager.cleanup_stale_jobs()
    manager.create("bench-noop", requested_by="isolated-benchmark")
    wal_peak = max(wal_peak, wal_path.stat().st_size if wal_path.exists() else 0)
wall_ms = (time.perf_counter() - wall_start) * 1000
cpu_ms = (time.process_time() - cpu_start) * 1000
io_after = Path("/proc/self/io").read_text().splitlines()
io_after = {key: int(value.strip()) for key, value in (line.split(":", 1) for line in io_after) if key in {"rchar", "wchar", "read_bytes", "write_bytes", "syscr", "syscw"}}
try:
    rss_after_kib = int(Path("/proc/self/status").read_text().split("VmRSS:")[1].split()[0])
except (OSError, ValueError, IndexError):
    rss_after_kib = None

print(json.dumps({
    "mode": mode,
    "repetitions": count,
    "wall_ms": round(wall_ms, 3),
    "process_cpu_ms": round(cpu_ms, 3),
    "process_ru_maxrss_kib_lifetime_before_after_not_operation_peak": {
        "before": ru_before,
        "after": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
    },
    "process_current_rss_kib_before_after": {"before": rss_before_kib, "after": rss_after_kib},
    "process_io_delta_bytes_and_calls": {key: io_after.get(key, 0) - io_before.get(key, 0) for key in io_after},
    "db_connections": connections,
    "stale_cleanup_calls": cleanup_calls,
    "sql_statement_counts": trace,
    "wal_peak_sampled_after_each_action_bytes": wal_peak,
    "db_wal_shm_bytes_after_cohort": {
        path.name: path.stat().st_size if path.exists() else 0
        for path in (db_path, wal_path, Path(f"{db_path}-shm"))
    },
    "provider_calls": {"requests": 0, "discoveries": 0, "mutations": 0},
    "native_runtime_calls": 0,
}, sort_keys=True))
