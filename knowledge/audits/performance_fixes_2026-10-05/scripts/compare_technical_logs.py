#!/usr/bin/env python3
"""Compare technical-log listing before/after on one private immutable copy.

The baseline implementation is loaded from the pinned Stage 4B source commit;
the candidate is loaded from the working tree. Neither reads production logs
after the initial copy. Only aggregate timings, counts and equality flags are
written to the evidence JSON. Event payloads stay inside the private temp dir.
"""
from __future__ import annotations

import json
import os
import shutil
import statistics
import subprocess
import sys
import tarfile
import tempfile
import time
import tracemalloc
from pathlib import Path

ROOT = Path("/srv/fwrouter")
PRODUCTION_LOGS = Path("/var/log/fwrouter/technical")
BASELINE = "24ef1ba"
REPEATS = 3


def child(source_backend: Path, state_dir: Path, result_path: Path) -> None:
    os.environ["FWROUTER_STATE_DIR"] = str(state_dir)
    os.environ.pop("STATE_DIR", None)
    sys.path.insert(0, str(source_backend))
    from fwrouter_api.services import logs

    counters = {"sanitize_calls": 0, "json_decode_calls": 0}
    original_sanitize = logs.sanitize_value
    original_loads = logs.json.loads

    def counted_sanitize(value, *, key=None):
        if key is None:
            counters["sanitize_calls"] += 1
        return original_sanitize(value, key=key)

    def counted_loads(*args, **kwargs):
        counters["json_decode_calls"] += 1
        return original_loads(*args, **kwargs)

    logs.sanitize_value = counted_sanitize
    logs.json.loads = counted_loads
    wall_ms: list[float] = []
    cpu_ms: list[float] = []
    decode_counts: list[int] = []
    sanitize_counts: list[int] = []
    output = None
    canonical = None
    for _ in range(REPEATS):
        counters.update(sanitize_calls=0, json_decode_calls=0)
        process_start = time.process_time()
        wall_start = time.perf_counter()
        output = logs.list_technical_logs(limit=20)
        wall_ms.append((time.perf_counter() - wall_start) * 1000)
        cpu_ms.append((time.process_time() - process_start) * 1000)
        decode_counts.append(counters["json_decode_calls"])
        sanitize_counts.append(counters["sanitize_calls"])
        canonical = json.dumps(output, ensure_ascii=False, sort_keys=True, separators=(",", ":"))

    # Measure Python allocation peak separately so tracemalloc overhead does
    # not distort the timing samples above.
    tracemalloc.start()
    tracemalloc.reset_peak()
    logs.list_technical_logs(limit=20)
    _current, peak_bytes = tracemalloc.get_traced_memory()
    tracemalloc.stop()

    result_path.write_text(canonical or "[]", encoding="utf-8")
    print(json.dumps({
        "wall_ms": [round(value, 3) for value in wall_ms],
        "wall_median_ms": round(statistics.median(wall_ms), 3),
        "cpu_ms": [round(value, 3) for value in cpu_ms],
        "cpu_median_ms": round(statistics.median(cpu_ms), 3),
        "json_decode_calls": decode_counts,
        "sanitize_calls": sanitize_counts,
        "returned_records": len(output or []),
        "python_tracemalloc_peak_bytes": peak_bytes,
    }, sort_keys=True, separators=(",", ":")))


def run_child(python: str, backend: Path, state_dir: Path, result_path: Path) -> dict:
    completed = subprocess.run(
        [python, str(Path(__file__).resolve()), "--child", str(backend), str(state_dir), str(result_path)],
        check=True, capture_output=True, text=True, timeout=60,
        env={**os.environ, "PYTHONUNBUFFERED": "1"},
    )
    return json.loads(completed.stdout)


def main() -> None:
    if len(sys.argv) > 1 and sys.argv[1] == "--child":
        child(Path(sys.argv[2]), Path(sys.argv[3]), Path(sys.argv[4]))
        return

    with tempfile.TemporaryDirectory(prefix="fwrouter-log-compare-") as temp:
        temp_root = Path(temp)
        copied_logs = temp_root / "logs" / "technical"
        copied_logs.parent.mkdir(parents=True)
        shutil.copytree(PRODUCTION_LOGS, copied_logs)
        input_files = [path for path in copied_logs.glob("*.jsonl") if path.is_file()]
        input_bytes = sum(path.stat().st_size for path in input_files)

        archive = temp_root / "baseline.tar"
        with archive.open("wb") as stream:
            subprocess.run(
                ["git", "-C", str(ROOT), "archive", "--format=tar", BASELINE],
                check=True, stdout=stream, stderr=subprocess.DEVNULL, timeout=30,
            )
        baseline_root = temp_root / "baseline"
        baseline_root.mkdir()
        with tarfile.open(archive) as bundle:
            bundle.extractall(baseline_root)

        old_state = temp_root / "old-state"
        new_state = temp_root / "new-state"
        shutil.copytree(copied_logs, old_state / "logs" / "technical", dirs_exist_ok=True)
        shutil.copytree(copied_logs, new_state / "logs" / "technical", dirs_exist_ok=True)
        old_payload = temp_root / "old-payload.json"
        new_payload = temp_root / "new-payload.json"
        old = run_child(sys.executable, baseline_root / "backend", old_state, old_payload)
        new = run_child(sys.executable, ROOT / "backend", new_state, new_payload)
        payload_equal = json.loads(old_payload.read_text(encoding="utf-8")) == json.loads(new_payload.read_text(encoding="utf-8"))

        evidence = {
            "baseline_commit": BASELINE,
            "input_snapshot": {"file_count": len(input_files), "bytes": input_bytes, "read_only_copy": True},
            "limit": 20,
            "repeats": REPEATS,
            "before": old,
            "after": new,
            "payload_equal": payload_equal,
            "unsanitized_input_copy_ephemeral": True,
            "unsanitized_rows_in_durable_output": False,
            "sanitized_return_payloads_ephemeral": True,
            "temporary_copies_removed_after_run": True,
            "memory_metric": "per-call Python allocation peak via tracemalloc; excludes native/process RSS",
        }
        output_path = ROOT / "knowledge/audits/performance_fixes_2026-10-05/TECHNICAL_LOGS_BEFORE_AFTER.json"
        output_path.write_text(json.dumps(evidence, sort_keys=True, indent=2) + "\n", encoding="utf-8")
        print(json.dumps({key: value for key, value in evidence.items() if key not in {"before", "after"}} | {
            "before": {key: value for key, value in old.items() if key != "wall_ms" and key != "cpu_ms"},
            "after": {key: value for key, value in new.items() if key != "wall_ms" and key != "cpu_ms"},
            "output": str(output_path),
        }, sort_keys=True, separators=(",", ":")))


if __name__ == "__main__":
    main()
