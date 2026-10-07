#!/opt/fwrouter-api/.venv/bin/python
"""Live benchmark for existing write_operational_log; emits 11 bounded audit records."""
from __future__ import annotations
import json
import os
import resource
import sys
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

ROOT = Path(os.environ.get("FWROUTER_API_ROOT", "/opt/fwrouter-api"))
sys.path.insert(0, str(ROOT))
from fwrouter_api.db import connection as db_connection
from fwrouter_api.services import logs

COUNTS = {"select": 0, "insert": 0, "update": 0, "delete": 0, "begin": 0, "commit": 0, "rollback": 0, "pragma": 0, "other": 0}
SQL_LOG = []

def classify_sql(statement: str) -> str:
    token = statement.lstrip().split(None, 1)[0].upper() if statement.strip() else ""
    key = {"SELECT": "select", "INSERT": "insert", "UPDATE": "update", "DELETE": "delete", "BEGIN": "begin", "COMMIT": "commit", "ROLLBACK": "rollback", "PRAGMA": "pragma"}.get(token, "other")
    COUNTS[key] += 1
    SQL_LOG.append(key)
    return key

@contextmanager
def traced_db_session():
    with db_connection.db_session() as conn:
        # Callback retains statement classes only; SQLite's expanded SQL is never stored.
        conn.set_trace_callback(classify_sql)
        yield conn


def process_io():
    result = {}
    try:
        for line in Path("/proc/self/io").read_text().splitlines():
            key, value = line.split(":", 1)
            if key in {"rchar", "wchar", "read_bytes", "write_bytes", "syscr", "syscw"}:
                result[key] = int(value.strip())
    except OSError:
        pass
    return result

def rss_bytes():
    try:
        for line in Path("/proc/self/status").read_text().splitlines():
            if line.startswith("VmRSS:"):
                return int(line.split()[1]) * 1024
    except OSError:
        pass
    return None

def run_phase(run_id: str, phase: str, count: int):
    rows = []
    for index in range(count):
        start_wall = time.perf_counter_ns()
        start_cpu = resource.getrusage(resource.RUSAGE_SELF)
        event = logs.write_operational_log(
            event_type="audit_live_benchmark_log_created",
            event_code="benchmark.audit_log_create",
            event_category="audit",
            message="Bounded live audit-log benchmark record.",
            level="info",
            component="fwrouter-live-benchmark",
            details={"benchmark": "live_operational_dataplane_2026-10-08", "run_id": run_id, "phase": phase, "sequence": index + 1},
        )
        end_cpu = resource.getrusage(resource.RUSAGE_SELF)
        rows.append({
            "latency_ms": round((time.perf_counter_ns() - start_wall) / 1e6, 3),
            "cpu_user_us": round((end_cpu.ru_utime - start_cpu.ru_utime) * 1e6),
            "cpu_system_us": round((end_cpu.ru_stime - start_cpu.ru_stime) * 1e6),
            "event_created": bool(event.get("event_id")) and not bool(event.get("deduplicated")),
        })
    return rows

def main():
    run_id = str(uuid.uuid4())
    logs.db_session = traced_db_session
    before_io = process_io()
    before_rss = rss_bytes()
    before_cpu = resource.getrusage(resource.RUSAGE_SELF)
    start = time.perf_counter_ns()
    single = run_phase(run_id, "single", 1)
    single_end_ns = time.perf_counter_ns()
    burst = run_phase(run_id, "burst10", 10)
    end_ns = time.perf_counter_ns()
    after_cpu = resource.getrusage(resource.RUSAGE_SELF)
    after_io = process_io()
    after_rss = rss_bytes()
    out = {
        "captured_at_unix_ns": time.time_ns(),
        "source_baseline": "6ba7f6f",
        "classification": "measured_live_mutation_authorized_audit_log_only",
        "run_id": run_id,
        "scope": "one real existing write_operational_log call plus bounded serial burst of ten; no cleanup/deletion",
        "single": single,
        "burst10": burst,
        "phase_wall_ms": {"single": round((single_end_ns - start) / 1e6, 3), "burst10": round((end_ns - single_end_ns) / 1e6, 3), "total": round((end_ns - start) / 1e6, 3)},
        "process_cpu_us_total": {"user": round((after_cpu.ru_utime - before_cpu.ru_utime) * 1e6), "system": round((after_cpu.ru_stime - before_cpu.ru_stime) * 1e6)},
        "process_rss_bytes_boundary": {"before": before_rss, "after": after_rss},
        "process_io_bytes_delta": {key: after_io.get(key, 0) - before_io.get(key, 0) for key in ("read_bytes", "write_bytes", "rchar", "wchar", "syscr", "syscw")},
        "sqlite_trace_counts": COUNTS,
        "sqlite_trace_statement_classes": SQL_LOG,
        "limitation": "process RSS is boundary-only; write_bytes is OS-attributed physical I/O and can remain zero with page cache; no synthetic request CPU or disk attribution",
        "secret_policy": "no client data; only synthetic run UUID and sequence metadata; no SQL text, response, identity, or payload stored",
    }
    print(json.dumps(out, ensure_ascii=False, indent=2))

if __name__ == "__main__":
    main()
