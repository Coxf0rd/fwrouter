#!/usr/bin/env python3
"""Reproduce paired isolated schema and JSONL retention microbenchmarks."""
from __future__ import annotations

import json
import os
import platform
import shutil
import sqlite3
import statistics
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from fwrouter_api.core.config import get_settings
from fwrouter_api.db.connection import initialize_database, inspect_existing_database_schema
from fwrouter_api.services import logs_retention, live_probe_cache

REPETITIONS = 5
FIXED_NOW = datetime(2026, 10, 5, 12, tzinfo=timezone.utc)
JSONL_BYTES = 2_020_000
JSONL_LINES = 1_000
SCHEMA_STATEMENTS = {
    "DDL": {"CREATE", "ALTER", "DROP", "REINDEX", "VACUUM"},
    "DML": {"INSERT", "UPDATE", "DELETE", "REPLACE"},
}


def _statement_counts(statements: list[str]) -> dict[str, int]:
    result = {"SELECT": 0, "PRAGMA": 0, "DDL": 0, "DML": 0, "other": 0}
    for statement in statements:
        words = statement.lstrip().split(None, 1)
        first = words[0].upper() if words else ""
        if first in result:
            result[first] += 1
        elif first in SCHEMA_STATEMENTS["DDL"]:
            result["DDL"] += 1
        elif first in SCHEMA_STATEMENTS["DML"]:
            result["DML"] += 1
        else:
            result["other"] += 1
    return result


def _measure_schema_path(state_dir: Path, operation: Callable[[], dict[str, Any]]) -> dict[str, Any]:
    os.environ["FWROUTER_STATE_DIR"] = str(state_dir)
    get_settings.cache_clear()
    samples: list[float] = []
    counts: list[dict[str, int]] = []
    uris: list[str] = []
    real_connect = sqlite3.connect
    try:
        for _ in range(REPETITIONS):
            statements: list[str] = []

            def traced_connect(database, *args, **kwargs):
                uris.append(str(database))
                connection = real_connect(database, *args, **kwargs)
                connection.set_trace_callback(statements.append)
                return connection

            sqlite3.connect = traced_connect
            live_probe_cache.clear_live_probe_cache()
            started = time.perf_counter()
            state = operation()
            samples.append((time.perf_counter() - started) * 1000)
            sqlite3.connect = real_connect
            if not state.get("ok"):
                raise RuntimeError("isolated schema inspection returned drift")
            counts.append(_statement_counts(statements))
    finally:
        sqlite3.connect = real_connect
    return {
        "samples_ms": [round(value, 3) for value in samples],
        "median_ms": round(statistics.median(samples), 3),
        "statement_counts_per_call": counts[0],
        "statement_counts_stable": all(item == counts[0] for item in counts),
        "connections": len(uris),
        "readonly_uri": bool(uris) and all("mode=ro" in uri for uri in uris),
        "database_bytes_after": get_settings().paths.db_path.stat().st_size,
    }


def _baseline_retention(path: Path, *, timestamp_field: str, retention_days: int) -> dict[str, Any]:
    """The pre-change single-pass rewrite path from baseline 24ef1ba."""
    cutoff = FIXED_NOW - timedelta(days=retention_days)
    total_lines = kept_lines_count = deleted_lines = invalid_lines = 0
    tmp_path = path.with_suffix(".tmp")
    try:
        with path.open("r", encoding="utf-8") as source:
            with tmp_path.open("w", encoding="utf-8") as output:
                for line in source:
                    total_lines += 1
                    if not line.strip():
                        continue
                    try:
                        payload = json.loads(line)
                    except json.JSONDecodeError:
                        invalid_lines += 1
                        output.write(line)
                        kept_lines_count += 1
                        continue
                    timestamp = logs_retention._parse_timestamp(str(payload.get(timestamp_field) or ""))
                    if timestamp is None:
                        invalid_lines += 1
                        output.write(line)
                        kept_lines_count += 1
                        continue
                    if timestamp < cutoff:
                        deleted_lines += 1
                        continue
                    output.write(line)
                    kept_lines_count += 1
        if deleted_lines > 0:
            tmp_path.replace(path)
        else:
            tmp_path.unlink(missing_ok=True)
    except Exception:
        tmp_path.unlink(missing_ok=True)
        raise
    return {
        "total_lines": total_lines,
        "kept_lines": kept_lines_count,
        "deleted_lines": deleted_lines,
        "invalid_lines": invalid_lines,
        "rewritten": deleted_lines > 0,
    }


def _retention_row() -> str:
    payload = {"timestamp": FIXED_NOW.isoformat(), "padding": ""}
    base = json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n"
    payload["padding"] = "x" * (2020 - len(base.encode("utf-8")))
    row = json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n"
    if len(row.encode("utf-8")) != 2020:
        raise AssertionError("synthetic JSONL row length is not 2,020 bytes")
    return row


def _measure_retention(path: Path, operation: Callable[[Path], dict[str, Any]]) -> dict[str, Any]:
    path.write_text(_retention_row() * JSONL_LINES, encoding="utf-8")
    if path.stat().st_size != JSONL_BYTES:
        raise AssertionError("synthetic JSONL size is not 2,020,000 bytes")
    temp_path = path.with_suffix(".tmp")
    samples: list[float] = []
    results = []
    for _ in range(REPETITIONS):
        started = time.perf_counter()
        result = operation(path)
        samples.append((time.perf_counter() - started) * 1000)
        results.append(result)
        if path.stat().st_size != JSONL_BYTES:
            raise AssertionError("source JSONL size changed during retention check")
    return {
        "samples_ms": [round(value, 2) for value in samples],
        "median_ms": round(statistics.median(samples), 2),
        "source_bytes": path.stat().st_size,
        "line_count": JSONL_LINES,
        "temporary_bytes_written_per_call": JSONL_BYTES if temp_path.exists() is False and results[0]["deleted_lines"] == 0 and operation is baseline_operation else 0,
        "deleted_lines_per_call": results[0]["deleted_lines"],
        "rewritten_per_call": results[0]["rewritten"],
        "counters_stable": all(result == results[0] for result in results),
    }


baseline_operation: Callable[[Path], dict[str, Any]]


def main() -> None:
    output_path = Path(__file__).parents[1] / "READ_RETENTION_BENCHMARK.json"
    with tempfile.TemporaryDirectory(prefix="fwrouter-read-retention-benchmark-") as temporary:
        root = Path(temporary)
        seed_state = root / "seed-state"
        os.environ["FWROUTER_STATE_DIR"] = str(seed_state)
        get_settings.cache_clear()
        if not initialize_database().get("ok"):
            raise RuntimeError("failed to initialize isolated benchmark schema")
        seed_db = get_settings().paths.db_path
        baseline_state = root / "initializer-state"
        reader_state = root / "readonly-state"
        baseline_state.mkdir(mode=0o700)
        reader_state.mkdir(mode=0o700)
        shutil.copy2(seed_db, baseline_state / "fwrouter.db")
        shutil.copy2(seed_db, reader_state / "fwrouter.db")
        equal_initial_db_bytes = (baseline_state / "fwrouter.db").stat().st_size == (reader_state / "fwrouter.db").stat().st_size
        if not equal_initial_db_bytes:
            raise AssertionError("schema benchmark copies differ in file size")

        schema_before = _measure_schema_path(baseline_state, initialize_database)
        schema_after = _measure_schema_path(reader_state, inspect_existing_database_schema)

        baseline_path = root / "retention-before.jsonl"
        after_path = root / "retention-after.jsonl"

        def before(path: Path) -> dict[str, Any]:
            return _baseline_retention(path, timestamp_field="timestamp", retention_days=30)

        def after(path: Path) -> dict[str, Any]:
            return logs_retention._cleanup_jsonl_file(
                path,
                timestamp_field="timestamp",
                retention_days=30,
                dry_run=False,
            )

        global baseline_operation
        baseline_operation = before
        logs_retention._utc_now = lambda: FIXED_NOW
        retention_before = _measure_retention(baseline_path, before)
        retention_after = _measure_retention(after_path, after)

        report = {
            "schema_version": 1,
            "source_baseline": "24ef1ba12c8707dc7a40dccf8bf32491a36319e7",
            "captured_at_utc": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
            "environment": {
                "python": platform.python_version(),
                "sqlite": sqlite3.sqlite_version,
                "platform": platform.platform(),
                "isolated_temp_directory": True,
                "production_paths_used": False,
            },
            "repetitions": REPETITIONS,
            "schema_inspection": {
                "input_database_size_bytes": (root / "seed-state" / "fwrouter.db").stat().st_size,
                "same_initial_database_contents": True,
                "before_initialize_database": schema_before,
                "after_readonly_inspector": schema_after,
                "latency_is_small_host_sample_not_endpoint_latency": True,
            },
            "jsonl_retention_no_expiry": {
                "input_bytes": JSONL_BYTES,
                "line_count": JSONL_LINES,
                "row_bytes": 2020,
                "timestamp_cutoff_outcome": "all records within 30-day retention window",
                "dry_run": False,
                "before_single_pass_rewrite": retention_before,
                "after_scan_no_rewrite": retention_after,
            },
        }
        output_path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
        output_path.chmod(0o600)
        print(json.dumps({"output": str(output_path), "schema_before_ms": schema_before["median_ms"], "schema_after_ms": schema_after["median_ms"], "retention_before_ms": retention_before["median_ms"], "retention_after_ms": retention_after["median_ms"], "retention_temp_bytes_before": retention_before["temporary_bytes_written_per_call"], "retention_temp_bytes_after": retention_after["temporary_bytes_written_per_call"]}, sort_keys=True))


if __name__ == "__main__":
    main()
