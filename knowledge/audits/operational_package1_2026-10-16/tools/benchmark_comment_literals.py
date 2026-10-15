#!/usr/bin/env python3
"""Private synthetic false-literal configured-scan benchmark; no production inputs."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import statistics
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

TARGET_BYTES = 22_700_000
DEFAULT_REPEATS = 3


def fixture(shape: str) -> str:
    false_literal_comments = "# secret:\n# listeners:\n# tun:\n# bind-address:\n"
    common_comment = "# secret:\n"
    actual_headers = (
        "mixed-port: 7890\n"
        "bind-address: 127.0.0.1\n"
        "tun:\n  enable: true\n"
        "listeners:\n"
        "  - name: fwrouter-transparent-redir\n"
        "    type: redir\n    listen: 127.0.0.1\n    port: 7892\n"
        "rules:\n"
    )
    if shape == "false_literals_no_headers":
        prefix = false_literal_comments + "rules:\n"
    elif shape == "secret_comment_early_headers":
        prefix = common_comment + actual_headers
    else:
        raise ValueError(shape)
    filler_line = "  - synthetic-rule-" + ("x" * 90) + "\n"
    remaining = TARGET_BYTES - len(prefix.encode("ascii"))
    count = remaining // len(filler_line)
    text = prefix + filler_line * count
    text += "x" * (TARGET_BYTES - len(text))
    if len(text.encode("ascii")) != TARGET_BYTES or not text.isascii():
        raise AssertionError("synthetic fixture has an unexpected byte length/encoding")
    return text


def normalized_output(value: dict[str, Any]) -> dict[str, Any]:
    normalized = dict(value)
    normalized["config_path"] = "<synthetic-config>"
    normalized["contours_path"] = "<synthetic-contours>"
    return normalized


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-root", required=True)
    parser.add_argument("--shape", required=True, choices=(
        "false_literals_no_headers", "secret_comment_early_headers",
    ))
    parser.add_argument("--repeats", type=int, default=DEFAULT_REPEATS)
    parser.add_argument("--out", required=True)
    args = parser.parse_args()
    os.environ.clear()
    os.environ["PYTHONHASHSEED"] = "0"
    source_root = Path(args.source_root).resolve()
    sys.path.insert(0, str(source_root / "backend"))
    from fwrouter_api.adapters.mihomo import MihomoHttpAdapter

    text = fixture(args.shape)
    out = Path(args.out).resolve()
    temp_parent = out.parent / "tmp"
    temp_parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="fwrouter-edge-", dir=temp_parent) as temp_root:
        root = Path(temp_root)
        config = root / "config.yaml"
        config.write_text(text, encoding="ascii")
        adapter = MihomoHttpAdapter(config_path=config, contours_path=root / "missing-contours.yaml")
        adapter._transparent_listener_socket_present = lambda **_: False  # type: ignore[method-assign]
        expected = normalized_output(adapter._config_runtime_details(config_text=text))
        wall_samples: list[float] = []
        cpu_samples: list[float] = []
        for _ in range(args.repeats):
            wall_start = time.perf_counter_ns()
            cpu_start = time.thread_time_ns()
            result = normalized_output(adapter._config_runtime_details(config_text=text))
            cpu_samples.append((time.thread_time_ns() - cpu_start) / 1e6)
            wall_samples.append((time.perf_counter_ns() - wall_start) / 1e6)
            if result != expected:
                raise AssertionError("output changed between repetitions")
        payload = {
            "source_root": str(source_root),
            "source_sha256": hashlib.sha256(
                (source_root / "backend/fwrouter_api/adapters/mihomo.py").read_bytes()
            ).hexdigest(),
            "python": sys.version,
            "python_executable": sys.executable,
            "environment_cleared": set(os.environ) == {"PYTHONHASHSEED"},
            "dotenv_loaded": False,
            "shape": args.shape,
            "fixture_bytes": len(text.encode("ascii")),
            "line_count": len(text.splitlines()),
            "repeats": args.repeats,
            "wall_ms": wall_samples,
            "thread_cpu_ms": cpu_samples,
            "wall_median_ms": statistics.median(wall_samples),
            "thread_cpu_median_ms": statistics.median(cpu_samples),
            "normalized_output": expected,
            "normalized_output_sha256": hashlib.sha256(
                json.dumps(expected, sort_keys=True, separators=(",", ":")).encode()
            ).hexdigest(),
            "socket_observation": "stubbed false; no network operation",
        }
    out.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
