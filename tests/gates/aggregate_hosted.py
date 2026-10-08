#!/usr/bin/env python3
"""Fail-closed aggregation for unit gate evidence and qualified hosted receipts."""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tests" / "gates"))
import gate  # noqa: E402
sys.path.insert(0, str(ROOT / "tests" / "acceptance"))
import provision  # noqa: E402


def read_json(path: Path, *, maximum: int = 8 * 1024 * 1024) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file() or path.stat().st_size > maximum:
        raise gate.GateError(f"evidence is missing, unsafe, or oversized: {path.name}")
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise gate.GateError(f"evidence root must be an object: {path.name}")
    return value


def verify_digest(value: dict[str, Any], field: str, label: str) -> None:
    digest = value.get(field)
    unsigned = dict(value)
    unsigned.pop(field, None)
    if not isinstance(digest, str) or digest != gate.canonical_digest(unsigned):
        raise gate.GateError(f"{label} digest is missing or invalid")


def functional_nodeids() -> set[str]:
    path = ROOT / "tests" / "acceptance" / "scenarios.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    rows = data.get("scenarios")
    if data.get("schema") != "fwrouter-acceptance-scenarios/v1" or not isinstance(rows, list):
        raise gate.GateError("functional scenario registry has an unsupported schema")
    selected = [row["nodeid"] for row in rows if row.get("suite") == "functional"]
    if not selected or len(selected) != len(set(selected)):
        raise gate.GateError("functional scenario registry is empty or contains duplicate IDs")
    return set(selected)


def validate_functional_receipt(path: Path, plan: dict[str, Any], expected: set[str]) -> list[str]:
    receipt = read_json(path)
    problems: list[str] = []
    if receipt.get("schema_version") != 1 or receipt.get("scope") != "hosted-native-process":
        problems.append("unsupported receipt schema or execution scope")
    if receipt.get("suite") != "functional" or receipt.get("status") != "passed":
        problems.append(f"suite status is {receipt.get('status', 'missing')}")
    if receipt.get("source_revision") != plan.get("source_commit"):
        problems.append("receipt source commit does not match the change plan")
    if receipt.get("plan_digest") != plan.get("plan_digest"):
        problems.append("receipt plan digest does not match the change plan")
    if receipt.get("container_confinement") != "passed" or receipt.get("runtime_preflight") != "passed":
        problems.append("host or container qualification did not pass")
    if receipt.get("cleanup") != "owned_resources_removed" or receipt.get("temporary_artifacts_removed") is not True:
        problems.append("owned cleanup is incomplete or unverified")
    nonce = receipt.get("suite_nonce")
    if not isinstance(nonce, str) or len(nonce) != 32 or any(char not in "0123456789abcdef" for char in nonce):
        problems.append("suite nonce is missing or malformed")
    tests = receipt.get("tests")
    if not isinstance(tests, dict):
        problems.append("JUnit test summary is absent")
        tests = {}
    nodeids = tests.get("nodeids", [])
    if set(nodeids) != expected or len(nodeids) != len(expected):
        problems.append("JUnit node IDs do not exactly match the functional registry")
    if any(tests.get(field) != 0 for field in ("failures", "errors", "skipped")):
        problems.append("JUnit contains failures, errors, or skips")
    if tests.get("tests") != len(expected):
        problems.append("JUnit case count differs from the functional registry")
    inner = receipt.get("application_receipt")
    if not isinstance(inner, dict):
        problems.append("application-owned receipt is absent")
        inner = {}
    if (inner.get("schema") != "fwrouter-application-acceptance-receipt/v2"
            or inner.get("scope") != "hosted-native-process"
            or inner.get("source_revision") != plan.get("source_commit")
            or inner.get("plan_digest") != plan.get("plan_digest")
            or inner.get("suite_nonce") != nonce
            or inner.get("profile_sha256") != receipt.get("profile_sha256")
            or inner.get("status") != "passed" or inner.get("exit_status") != 0
            or inner.get("cleanup_errors") != []):
        problems.append("application receipt is not bound to this source/profile/run")
    inner_rows = inner.get("tests")
    if not isinstance(inner_rows, list):
        problems.append("application exact-node receipt is absent")
    else:
        inner_ids = {row.get("nodeid") for row in inner_rows if isinstance(row, dict)}
        if inner_ids != expected or len(inner_rows) != len(expected):
            problems.append("application receipt node IDs do not exactly match the functional registry")
        for row in inner_rows:
            phases = row.get("phases") if isinstance(row, dict) else None
            if (not isinstance(row, dict) or row.get("status") != "passed"
                    or not isinstance(phases, dict) or set(phases) != {"setup", "call", "teardown"}
                    or any(phase != "passed" for phase in phases.values())):
                problems.append("application receipt includes a failed, skipped, or incomplete phase")
                break
    profile = receipt.get("profile")
    if not isinstance(profile, dict) or receipt.get("profile_sha256") is None:
        problems.append("pinned execution profile or profile digest is absent")
    else:
        encoded_profile = (json.dumps(profile, sort_keys=True, indent=2) + "\n").encode("utf-8")
        if hashlib.sha256(encoded_profile).hexdigest() != receipt.get("profile_sha256"):
            problems.append("profile digest does not match the archived profile fields")
        if (profile.get("schema") != "fwrouter-acceptance-profile/v2"
                or profile.get("source_revision") != plan.get("source_commit")
                or profile.get("plan_digest") != plan.get("plan_digest")):
            problems.append("profile source or plan binding is invalid")
        xray = profile.get("xray") if isinstance(profile.get("xray"), dict) else {}
        mihomo = profile.get("mihomo") if isinstance(profile.get("mihomo"), dict) else {}
        chromium = profile.get("chromium") if isinstance(profile.get("chromium"), dict) else {}
        pinned = (
            xray.get("version") == provision.INPUTS["xray"]["version"],
            xray.get("sha256") == provision.INPUTS["xray"]["sha256"],
            mihomo.get("version") == provision.INPUTS["mihomo"]["version"],
            mihomo.get("sha256") == provision.INPUTS["mihomo"]["sha256"],
            chromium.get("version") == provision.CHROMIUM_VERSION,
            chromium.get("bundle_sha256") == provision.INPUTS["chromium"]["sha256"],
            isinstance(chromium.get("sha256"), str)
            len(chromium.get("sha256", "")) == 64
            and all(char in "0123456789abcdef" for char in chromium.get("sha256", "")),
            profile.get("playwright_python") == provision.PLAYWRIGHT_VERSION,
        )
        if not all(pinned):
            problems.append("native/browser profile does not match exact pinned runtime inputs")
    return problems


def aggregate(plan_path: Path, unit_path: Path, hosted_paths: list[Path], *, minimum_repeats: int) -> dict[str, Any]:
    manifest = gate.load_manifest()
    plan = read_json(plan_path)
    gate.ensure_plan(plan, manifest)
    unit = read_json(unit_path)
    verify_digest(unit, "report_digest", "unit report")
    if (unit.get("source_commit") != plan.get("source_commit")
            or unit.get("plan_digest") != plan.get("plan_digest")
            or unit.get("manifest_version") != manifest.get("version")
            or unit.get("manifest_digest") != gate.canonical_digest(manifest)):
        raise gate.GateError("unit report does not match source, plan, and manifest")

    rows = {row["path"]: row for row in manifest["test_files"]}
    selected = set(plan["selected_files"])
    deferred = {path for path in selected if rows[path].get("execution_profile")}
    qualified_child = sorted(path for path in deferred
                             if rows[path].get("execution_profile") == "qualified-child-process")
    required_hosted = sorted(path for path in deferred
                             if rows[path].get("execution_profile") == "hosted-isolated-compose")
    observed_deferred = set(unit.get("deferred_remote_suites", []))
    if observed_deferred != deferred:
        raise gate.GateError("unit report did not defer exactly the profile-bound suites")
    suite_rows = [entry for entry in unit.get("executions", []) if entry.get("path") != "@L0"]
    by_path = {entry.get("path"): entry for entry in suite_rows}
    if len(by_path) != len(suite_rows) or set(by_path) != selected:
        raise gate.GateError("unit report suite coverage is incomplete or duplicated")
    for path, entry in by_path.items():
        required_status = "deferred_remote_hosted" if path in deferred else "passed"
        if entry.get("status") != required_status:
            raise gate.GateError(f"unit execution status is not acceptable for {path}")
    if unit.get("l0", {}).get("status") != "passed" or "L0" not in unit.get("completed_levels", []):
        raise gate.GateError("unit report lacks a completed L0 gate")

    problems: list[str] = []
    if unit.get("blocked_reasons"):
        problems.extend(f"unit: {reason}" for reason in unit["blocked_reasons"])
    if qualified_child:
        problems.append("NOT RUN: no qualified child-process executor for: " + ", ".join(qualified_child))

    hosted_ids = functional_nodeids()
    receipts: list[dict[str, Any]] = []
    if required_hosted:
        if len(hosted_paths) < minimum_repeats:
            problems.append(f"functional hosted receipts: {len(hosted_paths)}; required: {minimum_repeats}")
        for path in hosted_paths:
            problems.extend(f"hosted {path.name}: {reason}" for reason in
                            validate_functional_receipt(path, plan, hosted_ids))
            receipts.append(read_json(path))
        if len({receipt.get("suite_nonce") for receipt in receipts}) != len(receipts):
            problems.append("hosted repetitions do not have distinct run nonces")
    elif hosted_paths:
        problems.append("unexpected hosted receipt supplied when no hosted profile is selected")

    merged_status = dict(unit.get("node_status", {}))
    if receipts:
        # Multiple clean repetitions corroborate the same cases. They are not
        # duplicate coverage; each report was independently validated above.
        for nodeid in hosted_ids:
            if nodeid in merged_status and merged_status[nodeid] != "passed":
                problems.append(f"hosted pass conflicts with unit status for {nodeid}")
            merged_status[nodeid] = "passed"
    baseline = gate.load_baseline(ROOT / manifest["baseline_policy_file"])
    classification = gate.classify(plan, {"node_status": merged_status}, baseline)
    if not classification.get("eligible"):
        for key in ("novel_failures", "baseline_failures_unapproved", "baseline_failures_skipped_or_uncollected"):
            values = classification.get(key, [])
            if values:
                problems.append(f"baseline {key}: " + ", ".join(values))

    completed_levels = set(unit.get("completed_levels", []))
    for path in required_hosted:
        completed_levels.add(rows[path]["primary_level"])
    missing_levels = sorted(set(plan.get("required_levels", [])) - completed_levels)
    if missing_levels:
        problems.append("required levels are incomplete: " + ", ".join(missing_levels))
    if deferred and not required_hosted and not qualified_child:
        problems.append("profile selection has no matching hosted receipt adapter")
    return {
        "schema_version": 1,
        "source_commit": plan["source_commit"],
        "plan_digest": plan["plan_digest"],
        "manifest_version": manifest["version"],
        "selected_files": sorted(selected),
        "deferred_profile_files": sorted(deferred),
        "qualified_child_process_not_run": qualified_child,
        "hosted_receipt_count": len(receipts),
        "hosted_repeats_required": minimum_repeats,
        "completed_levels": sorted(completed_levels),
        "missing_levels": missing_levels,
        "baseline_classification": classification,
        "eligible": not problems,
        "blocked_reasons": problems,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", required=True, type=Path)
    parser.add_argument("--unit-report", required=True, type=Path)
    parser.add_argument("--hosted-report", action="append", default=[], type=Path)
    parser.add_argument("--minimum-repeats", type=int, default=1, choices=(1, 2))
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    try:
        result = aggregate(args.plan, args.unit_report, args.hosted_report,
                           minimum_repeats=args.minimum_repeats)
        if args.output:
            gate.atomic_json(args.output, result)
        print(json.dumps(result, sort_keys=True, indent=2))
        return 0 if result["eligible"] else 1
    except (gate.GateError, OSError, ValueError, KeyError, json.JSONDecodeError) as exc:
        print(f"aggregate: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
