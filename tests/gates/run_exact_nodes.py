#!/usr/bin/env python3
"""Run an explicit pytest node list under the normal isolated gate environment."""
from __future__ import annotations

import argparse
import json
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tests" / "gates"))
import gate  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--junit", required=True, type=Path)
    parser.add_argument("--report", required=True, type=Path)
    parser.add_argument("--log", required=True, type=Path)
    parser.add_argument("nodeids", nargs="+")
    args = parser.parse_args()
    if any("::" not in nodeid or nodeid.startswith("-") for nodeid in args.nodeids):
        parser.error("each positional argument must be an exact pytest node ID")
    if len(set(args.nodeids)) != len(args.nodeids):
        parser.error("duplicate exact node IDs are forbidden")
    if any(path.is_symlink() for path in (args.junit, args.report, args.log)):
        raise SystemExit("artifact paths cannot be symlinks")

    owned = Path(tempfile.mkdtemp(prefix="fwrouter-exact-nodes-", dir="/tmp"))
    try:
        env = gate.clean_test_environment(owned)
        env["PYTHONPATH"] = str(ROOT / "backend" / "tests") + ":" + str(ROOT / "backend")
        suite_root, reports_root = gate.create_suite_root(owned, "exact-node-baseline")
        node_report = reports_root / "nodes.json"
        junit_inside = reports_root / "results.xml"
        env["FWROUTER_PYTEST_COORDINATOR_ROOT"] = str(suite_root.resolve())
        env["FWROUTER_GATE_NODE_REPORT"] = str(node_report.resolve())
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.log.parent.mkdir(parents=True, exist_ok=True)
        command = [
            sys.executable, "-m", "pytest", "-p", "gate_plugin", "-p", "no:cacheprovider",
            "--basetemp", str(suite_root / "pytest-tmp"), "-q", "--tb=short",
            f"--junitxml={junit_inside}", *args.nodeids,
        ]
        code, output, truncated = gate.run_process(
            command, timeout=840, output_limit=4 * 1024 * 1024,
            cwd=ROOT / "backend", env=env,
        )
        args.log.write_text(output, encoding="utf-8")
        observed: dict[str, str] = {}
        if node_report.is_file() and node_report.stat().st_size <= 2 * 1024 * 1024:
            payload: Any = json.loads(node_report.read_text(encoding="utf-8"))
            if payload.get("schema_version") == 1 and isinstance(payload.get("node_status"), dict):
                observed = payload["node_status"]
        expected = {gate.canonical_node_id(nodeid) for nodeid in args.nodeids}
        exact = set(observed) == expected and all(
            status in {"passed", "failed", "skipped"} for status in observed.values()
        )
        counts = {name: sum(status == name for status in observed.values())
                  for name in ("passed", "failed", "skipped")}
        report = {
            "schema_version": 1,
            "scope": "exact-node-isolated-pytest",
            "expected_nodeids": sorted(expected),
            "observed_node_status": dict(sorted(observed.items())),
            "counts": counts,
            "pytest_exit_code": code,
            "output_truncated": truncated,
            "junit_present": junit_inside.is_file(),
            "exact_node_coverage": exact,
            "owned_temp_cleanup": False,
        }
        args.junit.parent.mkdir(parents=True, exist_ok=True)
        if junit_inside.is_file():
            shutil.copyfile(junit_inside, args.junit)
        shutil.rmtree(owned)
        report["owned_temp_cleanup"] = not owned.exists()
        report["junit_present"] = args.junit.is_file()
        args.report.write_text(json.dumps(report, sort_keys=True, indent=2) + "\n", encoding="utf-8")
        return 0 if (code == 0 and not truncated and counts["failed"] == 0
                     and counts["skipped"] == 0 and exact and report["junit_present"]
                     and report["owned_temp_cleanup"]) else 1
    finally:
        if owned.exists():
            shutil.rmtree(owned, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
