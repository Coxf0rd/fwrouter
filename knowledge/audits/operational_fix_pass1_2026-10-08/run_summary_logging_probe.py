#!/usr/bin/env python3
"""Run paired isolated summary/logging probes against baseline and current source."""

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path


HERE = Path(__file__).resolve().parent
PROBE = HERE / "summary_logging_probe.py"
PYTHON = "/opt/fwrouter-api/.venv/bin/python"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--baseline", required=True, type=Path)
    parser.add_argument("--current", required=True, type=Path)
    parser.add_argument("--repetitions", type=int, default=3)
    parser.add_argument("--output", type=Path, default=HERE / "summary_logging_raw.json")
    args = parser.parse_args()
    results = []
    for mode in ("summary", "logging"):
        for label, source in (("baseline", args.baseline), ("current", args.current)):
            for replicate in range(1, args.repetitions + 1):
                env = os.environ.copy()
                env["FWROUTER_SOURCE_ROOT"] = str(source.resolve())
                proc = subprocess.run(
                    [PYTHON, str(PROBE), mode],
                    env=env,
                    check=True,
                    text=True,
                    capture_output=True,
                )
                result = json.loads(proc.stdout.strip().splitlines()[-1])
                results.append(
                    {"side": label, "mode": mode, "replicate": replicate, "result": result}
                )
    args.output.write_text(json.dumps(results, indent=2, sort_keys=True) + "\n")


if __name__ == "__main__":
    main()
