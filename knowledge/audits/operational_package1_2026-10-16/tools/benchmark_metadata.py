#!/usr/bin/env python3
"""Isolated synthetic benchmark for FWRouter Mihomo configured metadata scans."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import statistics
import sys
import tempfile
import time
import tracemalloc
from pathlib import Path
from typing import Any


def fixture(size: int, shape: str) -> str:
    if size <= 0:
        return ""
    if shape == "early-header":
        prefix = (
            "mixed-port: 7890\n"
            "bind-address: 127.0.0.1\n"
            "tun:\n  enable: true\n"
            "listeners:\n"
            "  - name: fwrouter-transparent-redir\n"
            "    type: redir\n    listen: 127.0.0.1\n    port: 7892\n"
            "proxies:\n"
        )
        section = "\n" + "".join(f"  - {idx:06d}-rule-name\n" for idx in range(500))
        filler = "  - rule-" + ("x" * 100) + "\n"
        suffix = filler * max(0, (size - len(prefix) - len(section)) // len(filler))
        output = prefix + section + suffix
    elif shape == "missing-section":
        prefix = "port: 7890\nmode: rule\nproxies:\n"
        filler = "  - rule-" + ("x" * 100) + "\n"
        output = prefix + filler * max(0, (size - len(prefix)) // len(filler))
    elif shape == "late-header":
        padding = "# padding\n" * 8192
        prefix = (
            "mixed-port: 7890\n"
            "bind-address: 127.0.0.1\n"
            "tun:\n  enable: true\n"
            "listeners:\n"
            "  - name: fwrouter-transparent-redir\n"
            "    type: redir\n    listen: 127.0.0.1\n    port: 7892\n"
            "proxies:\n"
        )
        filler = "  - rule-" + ("x" * 100) + "\n"
        output = padding + prefix + filler * max(0, (size - len(padding) - len(prefix)) // len(filler))
    else:
        raise ValueError(shape)
    return output[:size]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--backend", required=True)
    parser.add_argument("--label", required=True)
    parser.add_argument("--repeats", type=int, default=7)
    parser.add_argument("--json-out")
    args = parser.parse_args()

    # Remove inherited application configuration; this script never loads dotenv.
    os.environ.clear()
    os.environ["PYTHONHASHSEED"] = "0"
    sys.path.insert(0, args.backend)
    from fwrouter_api.adapters import mihomo as mihomo_module
    from fwrouter_api.adapters.mihomo import MihomoHttpAdapter

    results: list[dict[str, Any]] = []
    workloads = [
        ("empty", 0),
        ("small", 32 * 1024),
        ("medium", 1024 * 1024),
        ("large", 22_700_000),
    ]
    def process_memory() -> dict[str, int | None]:
        values: dict[str, int | None] = {"VmRSS": None, "VmHWM": None}
        try:
            for line in Path("/proc/self/status").read_text(encoding="ascii").splitlines():
                key, _, value = line.partition(":")
                if key in values:
                    values[key] = int(value.strip().split()[0])
        except (OSError, ValueError, IndexError):
            pass
        return values

    with tempfile.TemporaryDirectory(prefix="fwrouter-package1-meta-") as temp_root:
        root = Path(temp_root)
        for label, size in workloads:
            shapes = ("early-header", "missing-section", "late-header") if label == "large" else (
                "early-header", "missing-section",
            )
            for shape in shapes:
                text = fixture(size, shape)
                config = root / f"{label}-{shape}.yaml"
                config.write_text(text, encoding="utf-8")
                adapter = MihomoHttpAdapter(config_path=config, contours_path=root / "missing-contours.yaml")
                adapter._transparent_listener_socket_present = lambda **_: False  # type: ignore[method-assign]
                calls = max(1, args.repeats)
                # Warm up imports and filesystem cache; these runs are discarded.
                expected = adapter._config_runtime_details(config_text=text)
                for _ in range(2):
                    adapter._config_runtime_details(config_text=text)
                wall_samples: list[float] = []
                cpu_samples: list[float] = []
                memory_before = process_memory()
                for _ in range(calls):
                    wall_start = time.perf_counter_ns()
                    cpu_start = time.thread_time_ns()
                    result = adapter._config_runtime_details(config_text=text)
                    cpu_samples.append((time.thread_time_ns() - cpu_start) / 1e6)
                    wall_samples.append((time.perf_counter_ns() - wall_start) / 1e6)
                    if result != expected:
                        raise AssertionError(f"unstable output in {label}/{shape}")
                tracemalloc.start()
                allocation_result = adapter._config_runtime_details(config_text=text)
                _, peak = tracemalloc.get_traced_memory()
                tracemalloc.stop()
                if allocation_result != expected:
                    raise AssertionError(f"unstable allocation-pass output in {label}/{shape}")
                scan_counts: dict[str, int] = {}
                if hasattr(mihomo_module, "_iter_config_lines"):
                    original_iterator = mihomo_module._iter_config_lines
                    line_count = 0
                    iterator_count = 0

                    def counted_iterator(value: str):
                        nonlocal line_count, iterator_count
                        iterator_count += 1
                        for item in original_iterator(value):
                            line_count += 1
                            yield item

                    mihomo_module._iter_config_lines = counted_iterator
                    try:
                        counted_result = adapter._config_runtime_details(config_text=text)
                    finally:
                        mihomo_module._iter_config_lines = original_iterator
                    if counted_result != expected:
                        raise AssertionError(f"unstable counted-pass output in {label}/{shape}")
                    fallback_calls = 0
                    if len(text) > 32 * 1024:
                        for marker in ("listeners:", "listeners:", "tun:", "bind-address:", "secret:"):
                            offset = text.find(marker)
                            if offset > 32 * 1024:
                                fallback_calls += 1
                    splitlines_calls = fallback_calls
                    if len(text) <= 32 * 1024:
                        splitlines_calls += iterator_count
                    scan_counts = {
                        "iterator_calls": iterator_count,
                        "yielded_lines": line_count,
                        "splitlines_calls": splitlines_calls,
                        "materialized_line_strings": splitlines_calls * len(text.splitlines()),
                    }
                else:
                    line_count = len(text.splitlines())
                    scan_counts = {
                        "splitlines_calls": 5,
                        "materialized_line_strings": 5 * line_count,
                    }
                results.append({
                    "size": label,
                    "bytes": len(text.encode("utf-8")),
                    "shape": shape,
                    "repeats": calls,
                    "wall_ms": wall_samples,
                    "cpu_ms": cpu_samples,
                    "tracemalloc_peak_bytes": peak,
                    "scan_counts": scan_counts,
                    "process_memory_before": memory_before,
                    "process_memory_after": process_memory(),
                    "result": expected,
                })
    payload = {
        "label": args.label,
        "backend": str(Path(args.backend).resolve()),
        "python": sys.version,
        "python_executable": sys.executable,
        "source_sha256": hashlib.sha256((Path(args.backend) / "fwrouter_api/adapters/mihomo.py").read_bytes()).hexdigest(),
        "environment_cleared": set(os.environ) == {"PYTHONHASHSEED"},
        "dotenv_loaded": False,
        "sockets_and_controllers": "not used; socket observation stubbed false",
        "config_scans_per_call": "source-coded baseline: listener ports, tun, transparent listeners, bind-address scalar, secret scalar; 5 independent splitlines calls",
        "metrics": results,
    }
    output = json.dumps(payload, indent=2, sort_keys=True)
    if args.json_out:
        Path(args.json_out).write_text(output + "\n", encoding="utf-8")
    print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
