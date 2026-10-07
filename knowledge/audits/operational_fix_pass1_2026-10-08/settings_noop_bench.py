#!/usr/bin/env python3
"""Matched isolated benchmark for repeated identical display-settings saves.

Run from backend with the project Python environment. Both cohorts use a
private temporary SQLite database. ``baseline`` restores the pre-fix
unconditional upsert behavior; ``fixed`` exercises the current implementation.
Prewarm/cache invalidation are counters so the benchmark measures their
dispatch decision, not asynchronous work.
"""
from __future__ import annotations

import json
import os
import resource
import tempfile
import time
from pathlib import Path

from fwrouter_api.core.config import Settings, get_settings
from fwrouter_api.core.paths import FWRouterPaths

ROOT = Path(tempfile.mkdtemp(prefix="fwrouter-pass1-settings-owned-")).resolve()
os.environ["FWROUTER_STATE_DIR"] = str(ROOT / "state")
os.environ["FWROUTER_ENVIRONMENT"] = "test"
os.environ["PYTHONDONTWRITEBYTECODE"] = "1"
Settings.model_config["env_file"] = None
Settings.paths = property(lambda _self: FWRouterPaths(
    etc_dir=ROOT / "etc", state_dir=ROOT / "state", log_dir=ROOT / "logs", run_dir=ROOT / "run"
))
get_settings.cache_clear()

from fwrouter_api.db import connection as db_connection  # noqa: E402
from fwrouter_api.db.connection import initialize_database  # noqa: E402
from fwrouter_api.services import ui_state_settings  # noqa: E402

initialize_database()
trace: dict[str, int] = {}
connections = 0
original_connect = db_connection.connect


def kind(statement: str) -> str:
    token = statement.strip().split(None, 1)[0].split("(", 1)[0].upper()
    return token if token in {"SELECT", "INSERT", "UPDATE", "DELETE", "REPLACE", "BEGIN", "COMMIT", "ROLLBACK", "PRAGMA"} else "OTHER"


def traced_connect():
    global connections
    connection = original_connect()
    connections += 1
    connection.set_trace_callback(lambda statement: trace.__setitem__(kind(statement), trace.get(kind(statement), 0) + 1))
    return connection


db_connection.connect = traced_connect
ui_state_settings.connect = traced_connect
dispatches = {"cache_clear": 0, "prewarm": 0}
ui_state_settings.clear_live_probe_cache = lambda: dispatches.__setitem__("cache_clear", dispatches["cache_clear"] + 1)
ui_state_settings.prime_runtime_read_models_async = lambda **_kwargs: dispatches.__setitem__("prewarm", dispatches["prewarm"] + 1)


def legacy_save(key: str, value: dict) -> bool:
    with db_connection.db_session() as connection:
        connection.execute(
            """
            INSERT INTO settings (key, value_json, updated_at)
            VALUES (?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(key) DO UPDATE SET
                value_json = excluded.value_json,
                updated_at = excluded.updated_at
            """,
            (key, ui_state_settings._json_dumps(value)),
        )
    return True


mode = os.environ.get("FWROUTER_PASS1_BENCH_MODE", "fixed")
if mode == "baseline":
    ui_state_settings._save_setting = legacy_save
elif mode != "fixed":
    raise SystemExit("FWROUTER_PASS1_BENCH_MODE must be baseline or fixed")

payload = {
    "system_visibility": {"lan": False},
    "show_inactive": True,
    "hidden_subject_ids": ["fixture:one"],
}
ui_state_settings.save_ui_display_settings(payload)
trace.clear()
connections = 0
dispatches.update(cache_clear=0, prewarm=0)
db = get_settings().paths.db_path
wal = Path(f"{db}-wal")
shm = Path(f"{db}-shm")
count = 100
ru_maxrss_before = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
io_before: dict[str, int] = {}
try:
    for line in Path("/proc/self/io").read_text().splitlines():
        key, value = line.split(":", 1)
        if key in {"rchar", "wchar", "read_bytes", "write_bytes", "syscr", "syscw"}:
            io_before[key] = int(value.strip())
except OSError:
    pass
rss_before_kib = None
try:
    rss_before_kib = int(Path("/proc/self/status").read_text().split("VmRSS:")[1].split()[0])
except (OSError, ValueError, IndexError):
    pass
wal_peak_bytes = wal.stat().st_size if wal.exists() else 0
wall_start = time.perf_counter()
cpu_start = time.process_time()
for _ in range(count):
    ui_state_settings.save_ui_display_settings(payload)
    wal_peak_bytes = max(wal_peak_bytes, wal.stat().st_size if wal.exists() else 0)
cpu_ms = (time.process_time() - cpu_start) * 1000
wall_ms = (time.perf_counter() - wall_start) * 1000

try:
    rss_kib = int(Path("/proc/self/status").read_text().split("VmRSS:")[1].split()[0])
except (OSError, ValueError, IndexError):
    rss_kib = None
io_after: dict[str, int] = {}
try:
    for line in Path("/proc/self/io").read_text().splitlines():
        key, value = line.split(":", 1)
        if key in {"rchar", "wchar", "read_bytes", "write_bytes", "syscr", "syscw"}:
            io_after[key] = int(value.strip())
except OSError:
    pass
io_delta = {key: io_after.get(key, 0) - io_before.get(key, 0) for key in io_after}

print(json.dumps({
    "mode": mode,
    "repetitions": count,
    "wall_ms": round(wall_ms, 3),
    "process_cpu_ms": round(cpu_ms, 3),
    "process_ru_maxrss_kib_lifetime_before_after_not_operation_peak": {
        "before": ru_maxrss_before,
        "after": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
    },
    "process_current_rss_kib_before_after": {"before": rss_before_kib, "after": rss_kib},
    "process_io_delta_bytes_and_calls": io_delta,
    "db_connections": connections,
    "sql_statement_counts": trace,
    "cache_clear_dispatches": dispatches["cache_clear"],
    "prewarm_dispatches": dispatches["prewarm"],
    "provider_calls": {"requests": 0, "discoveries": 0, "mutations": 0},
    "native_runtime_calls": 0,
    "wal_peak_sampled_after_each_action_bytes": wal_peak_bytes,
    "db_wal_shm_bytes_after_cohort": {path.name: path.stat().st_size if path.exists() else 0 for path in (db, wal, shm)},
}, sort_keys=True))
