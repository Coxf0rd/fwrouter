#!/usr/bin/env python3
"""One-shot, bounded /system/summary profiler using a read-only DB snapshot.

The process reads the production native/file contour but directs all SQLite
reads to a private online-backup copy opened with SQLite mode=ro. It prints
timings/counts only; never prints config, DB rows, command arguments or output.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import threading
import time
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, "/srv/fwrouter/backend")

SOURCE_DB = Path("/var/lib/fwrouter-v2/fwrouter.db")
LIVE_STATE = Path("/var/lib/fwrouter-v2")
locks = threading.Lock()
timings: dict[str, list[float]] = defaultdict(list)
sql_timings: dict[str, list[float]] = defaultdict(list)
subprocess_timings: dict[str, list[float]] = defaultdict(list)


def record(bucket: dict[str, list[float]], key: str, elapsed: float) -> None:
    with locks:
        bucket[key].append(elapsed * 1000)


class ReadOnlyTimedConnection:
    def __init__(self, connection: sqlite3.Connection):
        self._connection = connection

    def execute(self, sql: str, parameters=()):
        started = time.perf_counter()
        try:
            return self._connection.execute(sql, parameters)
        finally:
            digest = hashlib.sha256(" ".join(sql.split()).encode()).hexdigest()[:12]
            record(sql_timings, digest, time.perf_counter() - started)

    def executemany(self, sql: str, parameters):
        started = time.perf_counter()
        try:
            return self._connection.executemany(sql, parameters)
        finally:
            digest = hashlib.sha256(" ".join(sql.split()).encode()).hexdigest()[:12]
            record(sql_timings, digest, time.perf_counter() - started)

    def __getattr__(self, name: str):
        return getattr(self._connection, name)


def wrap_call(module, name: str):
    original = getattr(module, name)

    def measured(*args, **kwargs):
        started = time.perf_counter()
        try:
            return original(*args, **kwargs)
        finally:
            record(timings, f"{module.__name__}.{name}", time.perf_counter() - started)

    setattr(module, name, measured)


def stats(values: list[float]) -> dict[str, float | int]:
    ordered = sorted(values)
    if not ordered:
        return {"count": 0}
    p95_index = max(0, min(len(ordered) - 1, int(0.95 * len(ordered) + 0.999) - 1))
    return {
        "count": len(ordered),
        "sum_ms": round(sum(ordered), 3),
        "max_ms": round(ordered[-1], 3),
        "p95_ms": round(ordered[p95_index], 3),
    }


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="fwrouter-summary-profile-") as temp:
        state = Path(temp) / "state"
        state.mkdir(mode=0o700)
        (state / "logs").mkdir(mode=0o700)
        for name in ("generated", "xray", "rules", "mihomo", "last-good"):
            live = LIVE_STATE / name
            if live.exists():
                (state / name).symlink_to(live, target_is_directory=True)

        db_path = state / "fwrouter.db"
        source = sqlite3.connect(f"file:{SOURCE_DB}?mode=ro", uri=True, timeout=10)
        destination = sqlite3.connect(db_path)
        source.backup(destination, pages=256, sleep=0.02)
        destination.close()
        source.close()
        db_path.chmod(0o400)
        os.environ["FWROUTER_STATE_DIR"] = str(state)

        from fwrouter_api.core.config import get_settings
        from fwrouter_api.db import connection as db_connection
        from fwrouter_api.routes import system as system_route
        from fwrouter_api.services import dataplane_status, runtime, system_summary
        from fwrouter_api.adapters.mihomo import DEFAULT_MIHOMO_ADAPTER

        get_settings.cache_clear()

        def connect_readonly():
            uri = f"file:{db_path.resolve()}?mode=ro"
            connection = sqlite3.connect(uri, uri=True, timeout=10)
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA query_only=ON")
            return ReadOnlyTimedConnection(connection)

        db_connection.connect = connect_readonly

        for module, names in (
            (system_route, ("inspect_database_schema", "build_system_summary")),
            (system_summary, ("_build_system_summary_uncached", "get_scoped_egress_runtime_summary")),
            (runtime, (
                "_build_runtime_summary", "_cached_mihomo_health", "_cached_xray_health",
                "probe_external_ingress_runtime", "get_subscription_state", "fetch_modules",
                "read_live_dataplane_payload", "get_routing_global_state",
                "list_subjects_effective_summaries",
            )),
            (dataplane_status, ("_read_live_dataplane_payload", "inspect_transparent_path_counters")),
        ):
            for name in names:
                wrap_call(module, name)

        original_config_details = DEFAULT_MIHOMO_ADAPTER._config_runtime_details

        def measured_config_details(*args, **kwargs):
            path = get_settings().paths.generated_dir / "mihomo" / "config.yaml"
            size = path.stat().st_size if path.exists() else 0
            started = time.perf_counter()
            result = original_config_details(*args, **kwargs)
            record(timings, f"mihomo._config_runtime_details.bytes={size}", time.perf_counter() - started)
            return result

        DEFAULT_MIHOMO_ADAPTER._config_runtime_details = measured_config_details

        import subprocess
        original_run = subprocess.run

        def measured_run(args, *positional, **kwargs):
            command = Path(str(args[0])).name if isinstance(args, (tuple, list)) and args else "unknown"
            started = time.perf_counter()
            try:
                return original_run(args, *positional, **kwargs)
            finally:
                record(subprocess_timings, command, time.perf_counter() - started)

        subprocess.run = measured_run
        started = time.perf_counter()
        response = system_route.system_summary()
        handler_ms = (time.perf_counter() - started) * 1000

        from fastapi.encoders import jsonable_encoder
        serialization_started = time.perf_counter()
        body = json.dumps(jsonable_encoder(response), ensure_ascii=False, separators=(",", ":")).encode()
        serialization_ms = (time.perf_counter() - serialization_started) * 1000
        report = {
            "scope": "one direct route invocation; DB online snapshot opened mode=ro/query_only; production generated/native contour read-only",
            "handler_wall_ms": round(handler_ms, 3),
            "response_bytes": len(body),
            "serialization_ms": round(serialization_ms, 3),
            "function_timings": {key: stats(values) for key, values in sorted(timings.items())},
            "sqlite_statements": {
                "count": sum(len(values) for values in sql_timings.values()),
                "total_ms": round(sum(sum(values) for values in sql_timings.values()), 3),
                "by_fingerprint": {key: stats(values) for key, values in sorted(sql_timings.items())},
            },
            "subprocesses": {key: stats(values) for key, values in sorted(subprocess_timings.items())},
        }
        print(json.dumps(report, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
