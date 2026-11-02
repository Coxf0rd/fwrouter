#!/usr/bin/env python3
"""Bounded disposable SQLite open microbenchmark for Package E evidence."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import sqlite3
import statistics
import sys
import tempfile
import time


ROOT = Path(__file__).resolve().parents[3]
BACKEND = ROOT / "backend"
sys.path.insert(0, str(BACKEND))


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _percentile(values: list[int], percentile: float) -> int:
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, int((percentile * len(ordered) + 0.999999)) - 1))
    return ordered[index]


def _baseline_open_close(path: Path) -> None:
    """Replay the pre-package connection PRAGMA/permissions routine."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.parent.chmod(0o700)
    if path.exists():
        path.chmod(0o600)
    connection = sqlite3.connect(path, timeout=30.0)
    try:
        connection.execute("PRAGMA foreign_keys = ON;")
        connection.execute("PRAGMA busy_timeout = 30000;")
        for attempt in range(6):
            try:
                connection.execute("PRAGMA journal_mode = WAL;")
                break
            except sqlite3.OperationalError as exc:
                if "database is locked" not in str(exc).lower() or attempt == 5:
                    raise
                time.sleep(0.2)
        connection.execute("PRAGMA synchronous = NORMAL;")
        connection.execute("PRAGMA temp_store = MEMORY;")
        path.chmod(0o600)
        for suffix in ("-wal", "-shm"):
            sidecar = Path(f"{path}{suffix}")
            if sidecar.exists():
                sidecar.chmod(0o600)
    finally:
        connection.close()


def main() -> None:
    output_path = Path(__file__).with_name("schema-open-benchmark.json")
    with tempfile.TemporaryDirectory(prefix="fwrouter-schema-open-bench-", dir="/tmp") as temporary:
        state_root = Path(temporary) / "state"
        state_root.mkdir(mode=0o700)
        os.environ["FWROUTER_STATE_DIR"] = str(state_root)

        from fwrouter_api.core.config import get_settings
        from fwrouter_api.db import connection as db_connection
        from fwrouter_api.db.connection import initialize_database

        get_settings.cache_clear()
        db_path = get_settings().paths.db_path
        initial = initialize_database()
        if not initial.get("ok"):
            raise RuntimeError("disposable canonical DB initialization failed")

        connection = db_connection.connect()
        connection.close()
        db_connection._clear_schema_admission_cache()
        started = time.perf_counter_ns()
        connection = db_connection.connect()
        connection.close()
        cold_first_open_ns = time.perf_counter_ns() - started

        paired: list[dict[str, int]] = []
        for pair_number in range(60):
            measured: dict[str, int] = {}
            order = ("candidate", "baseline") if pair_number % 2 == 0 else ("baseline", "candidate")
            for implementation in order:
                started = time.perf_counter_ns()
                if implementation == "candidate":
                    connection = db_connection.connect()
                    connection.close()
                else:
                    _baseline_open_close(db_path)
                measured[f"{implementation}_ns"] = time.perf_counter_ns() - started
            paired.append(measured)

        candidate_values = [row["candidate_ns"] for row in paired]
        baseline_values = [row["baseline_ns"] for row in paired]
        source_paths = {
            "connection.py": ROOT / "backend/fwrouter_api/db/connection.py",
            "schema_state.py": ROOT / "backend/fwrouter_api/db/schema_state.py",
            "schema.sql": ROOT / "backend/fwrouter_api/db/schema.sql",
        }
        report = {
            "scope": "one disposable canonical schema-27 SQLite database; open-close latency only",
            "pair_count": len(paired),
            "warmup": "one candidate open before cache clear; one separately timed cold candidate admission; measured pairs interleaved in alternating order",
            "cold_first_candidate_open_ns": cold_first_open_ns,
            "measure": "time.perf_counter_ns around each connection open+close; baseline replays baseline permissions and SQLite PRAGMA routine without schema validation",
            "candidate_ns": {
                "median": int(statistics.median(candidate_values)),
                "p95_nearest_rank": _percentile(candidate_values, 0.95),
                "samples": candidate_values,
            },
            "baseline_ns": {
                "median": int(statistics.median(baseline_values)),
                "p95_nearest_rank": _percentile(baseline_values, 0.95),
                "samples": baseline_values,
            },
            "source_sha256": {name: _sha256(path) for name, path in source_paths.items()},
            "baseline_source_sha256": {
                "connection.py": "42f2c1f64da3a8974ad5a2787886f7616e0523972bed1be472827d7abff9c682",
            },
            "limits": [
                "Synthetic local microbenchmark only; not production latency evidence.",
                "No CPU, RSS, allocation, disk-write, IOPS, or resource-use conclusions are measured.",
            ],
        }
        output_path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
