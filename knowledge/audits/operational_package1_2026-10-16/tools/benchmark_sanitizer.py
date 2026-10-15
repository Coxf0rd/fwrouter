#!/usr/bin/env python3
"""CPU-only synthetic benchmark for the FWRouter event sanitizer.

Run with a source root to keep baseline and candidate imports isolated:
  python benchmark_sanitizer.py /path/to/source-root baseline.json
No settings, dotenv, database, production files, or network are used.
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import re
import sys
import time
import tracemalloc
from pathlib import Path
from typing import Any

REPEATS = 5
CLASSIFICATION_CALLS = 100_000
RECORDS_PER_SIZE = 600


def load_module(path: Path):
    spec = importlib.util.spec_from_file_location("bench_event_contract", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load module: {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def records(size: str) -> list[dict[str, Any]]:
    padding = "synthetic diagnostic payload " * (1 if size == "small" else 55)
    result = []
    for i in range(RECORDS_PER_SIZE):
        result.append({
            "event_id": f"event-synth-{i:06d}",
            "event_type": "runtime_probe_completed",
            "created_at": "2026-10-16T12:34:56+00:00",
            "component": "synthetic-component",
            "level": "warning",
            "message": f"probe finished for synthetic node {i}; {padding}",
            "details": {
                "request_id": f"request-synth-{i:06d}",
                "job_id": f"job-synth-{i:06d}",
                "server_id": f"server-synth-{i % 17:02d}",
                "attempt_id": i,
                "nested": {
                    "endpoint": "https://user:synthetic-password@example.invalid/path?token=synthetic-token",
                    "authorization": "Bearer synthetic-bearer-token",
                    "labels": ["ordinary", "api_key=synthetic-api-key", padding[:80]],
                    "metrics": {"elapsed_ms": i % 250, "success": i % 2 == 0},
                },
            },
        })
    return result


def measure(fn):
    wall_start = time.perf_counter()
    cpu_start = time.process_time()
    result = fn()
    cpu = time.process_time() - cpu_start
    wall = time.perf_counter() - wall_start
    return {"wall_seconds": wall, "cpu_seconds": cpu}, result


def peak_allocations(fn):
    tracemalloc.start()
    fn()
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    return peak


def digest(value: Any) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def main() -> None:
    source_root = Path(sys.argv[1]).resolve()
    module_path = source_root / "backend/fwrouter_api/services/event_contract.py"
    output_path = Path(sys.argv[2]).resolve()
    module = load_module(module_path)
    keys = [
        "request_id", "job-id", "serverId", "entity_id", "event_id",
        "password", "access_token", "refreshToken", "private-key", "api.key",
        "correlation_id", "workflow_id", "safe_custom_field", "client_uuid",
        "token_id", "endpoint", "Authorization", "nested_secret",
    ]
    classification_samples = [keys[i % len(keys)] for i in range(CLASSIFICATION_CALLS)]
    workload = {name: records(name) for name in ("small", "large")}
    results = {
        "source": str(module_path),
        "repeats": REPEATS,
        "classification_calls_per_run": CLASSIFICATION_CALLS,
        "records_per_size_per_run": RECORDS_PER_SIZE,
        "workloads": {},
    }

    def classify():
        return [module._sensitive_key(key) for key in classification_samples]

    class_runs = []
    class_reference = None
    for _ in range(REPEATS):
        metrics, output = measure(classify)
        metrics["output_sha256"] = digest(output)
        class_reference = class_reference or metrics["output_sha256"]
        if metrics["output_sha256"] != class_reference:
            raise AssertionError("classification output changed between repetitions")
        class_runs.append(metrics)
    results["workloads"]["key_classification_peak_alloc_bytes"] = peak_allocations(classify)
    results["workloads"]["key_classification"] = class_runs

    for size, input_records in workload.items():
        runs = []
        expected_digest = None
        def sanitize_all():
            return [module.sanitize_value(record) for record in input_records]
        for _ in range(REPEATS):
            metrics, output = measure(sanitize_all)
            metrics.update({
                "output_sha256": digest(output),
                "input_bytes": sum(len(json.dumps(item, ensure_ascii=False).encode()) for item in input_records),
            })
            expected_digest = expected_digest or metrics["output_sha256"]
            if metrics["output_sha256"] != expected_digest:
                raise AssertionError(f"{size} output changed between repetitions")
            runs.append(metrics)
        results["workloads"][f"nested_{size}_records"] = runs
        results["workloads"][f"nested_{size}_peak_alloc_bytes"] = peak_allocations(sanitize_all)

    original_sub = re.sub
    sub_calls = 0
    def counted_sub(*args, **kwargs):
        nonlocal sub_calls
        sub_calls += 1
        return original_sub(*args, **kwargs)
    re.sub = counted_sub
    try:
        counted_classification = [module._sensitive_key(key) for key in classification_samples]
    finally:
        re.sub = original_sub
    results["instrumented_reference"] = {
        "operation": "100000 _sensitive_key calls",
        "re_sub_calls": sub_calls,
        "classified_sensitive": sum(counted_classification),
        "instrumented": True,
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(results, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(json.dumps(results, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
