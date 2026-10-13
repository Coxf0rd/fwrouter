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
import launcher as acceptance_launcher  # noqa: E402


def read_json(path: Path, *, maximum: int = 8 * 1024 * 1024) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file() or path.stat().st_size > maximum:
        raise gate.GateError(f"evidence is missing, unsafe, or oversized: {path.name}")
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise gate.GateError(f"evidence root must be an object: {path.name}")
    return value


def validate_runtime_inputs(value: dict[str, Any]) -> None:
    native = value.get("native") if isinstance(value.get("native"), dict) else {}
    chromium = value.get("chromium") if isinstance(value.get("chromium"), dict) else {}
    valid = (
        value.get("schema") == "fwrouter-hosted-inputs/v1"
        and value.get("base_image") == provision.BASE_IMAGE
        and value.get("playwright") == provision.PLAYWRIGHT_VERSION
        and all(isinstance(native.get(name), dict) for name in ("xray", "mihomo"))
        and all(native[name].get("version") == provision.INPUTS[name]["version"]
                and native[name].get("source_archive_sha256") == provision.INPUTS[name]["sha256"]
                and native[name].get("binary_sha256") == provision.BINARY_SHA256[name]
                for name in ("xray", "mihomo"))
        and chromium.get("version") == provision.CHROMIUM_VERSION
        and chromium.get("source_archive_sha256") == provision.INPUTS["chromium"]["sha256"]
        and chromium.get("bundle_sha256") == provision.CHROMIUM_BUNDLE_SHA256
        and chromium.get("executable_sha256") == provision.CHROMIUM_BINARY_SHA256
    )
    if not valid:
        raise gate.GateError("hosted runtime input manifest differs from exact verified pins")


def verify_digest(value: dict[str, Any], field: str, label: str) -> None:
    digest = value.get(field)
    unsigned = dict(value)
    unsigned.pop(field, None)
    if not isinstance(digest, str) or digest != gate.canonical_digest(unsigned):
        raise gate.GateError(f"{label} digest is missing or invalid")


def unit_failure_report(plan_path: Path, unit_path: Path, unit_exit_code: int) -> dict[str, Any]:
    """Publish bounded failure evidence when the routine unit gate stops the bundle."""
    if not isinstance(unit_exit_code, int) or isinstance(unit_exit_code, bool) or not 1 <= unit_exit_code <= 255:
        raise gate.GateError("unit failure report requires a nonzero process exit code")
    manifest = gate.load_manifest()
    plan = read_json(plan_path)
    gate.ensure_plan(plan, manifest, allow_l6=bool(plan.get("full_suite_required")))
    unit = read_json(unit_path)
    verify_digest(unit, "report_digest", "unit report")
    if (unit.get("source_commit") != plan.get("source_commit")
            or unit.get("plan_digest") != plan.get("plan_digest")
            or unit.get("manifest_version") != manifest.get("version")
            or unit.get("manifest_digest") != gate.canonical_digest(manifest)):
        raise gate.GateError("unit report does not match source, plan, and manifest")
    reasons = unit.get("blocked_reasons", [])
    if not isinstance(reasons, list):
        raise gate.GateError("unit report blocked reasons are malformed")
    summary = {
        "schema_version": 1,
        "report_kind": "routine-unit-failure",
        "aggregate_status": "blocked",
        "eligible": False,
        "source_commit": plan["source_commit"],
        "plan_digest": plan["plan_digest"],
        "unit_report_digest": unit["report_digest"],
        "unit_exit_code": unit_exit_code,
        "unit_blocked_reasons": [gate.redact_failure_output(str(item))[:1024] for item in reasons[:32]],
        "not_run": ["runtime-input-provisioning", "hosted-functional-profile-bundle",
                    "hosted-receipt-aggregation"],
    }
    summary["report_digest"] = gate.canonical_digest(summary)
    return summary


def aggregate_error_report(error: Exception) -> dict[str, Any]:
    """Represent an aggregate validation exception without implying a receipt."""
    summary = {
        "schema_version": 1,
        "report_kind": "aggregate-error",
        "aggregate_status": "invalid-or-incomplete-evidence",
        "eligible": False,
        "error_type": type(error).__name__[:80],
        "error_summary": gate.redact_failure_output(str(error))[:1024],
    }
    summary["report_digest"] = gate.canonical_digest(summary)
    return summary


def run_nonces_are_distinct(nonces: list[str]) -> bool:
    """Receipt families share one run identity namespace after validation."""
    return len(nonces) == len(set(nonces))


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


def hosted_partition_nodeids() -> dict[str, set[str]]:
    scenarios = json.loads((ROOT / "tests" / "acceptance" / "scenarios.json").read_text(encoding="utf-8"))
    rows = scenarios.get("scenarios") if isinstance(scenarios, dict) else None
    if (not isinstance(scenarios, dict)
            or scenarios.get("schema") != "fwrouter-acceptance-scenarios/v1"
            or not isinstance(rows, list)):
        raise gate.GateError("hosted profile partition registry has an unsupported schema")
    functional = functional_nodeids()
    packet = {row.get("nodeid") for row in rows
              if isinstance(row, dict) and row.get("suite") == "packet"}
    provider = set(gate.PROVIDER_COHORT_NODEIDS)
    ordinary = functional - provider
    partitions = {"functional-ordinary": ordinary, "provider-cohort": provider,
                  "packet-diagnostic": packet}
    all_ids = [nodeid for ids in partitions.values() for nodeid in ids]
    if (len(functional) != 37 or len(ordinary) != 26 or len(provider) != 11 or len(packet) != 2
            or not provider <= functional or functional & packet or len(all_ids) != len(set(all_ids))
            or ordinary | provider != functional or ordinary | provider | packet != functional | packet):
        raise gate.GateError("hosted profiles are not a disjoint exhaustive 26+11+2 partition")
    return partitions


def main_smoke_nodeid() -> str:
    """Resolve the native application smoke node through the checked-in registry."""
    scenarios = json.loads((ROOT / "tests" / "acceptance" / "scenarios.json").read_text(encoding="utf-8"))
    rows = scenarios.get("scenarios") if isinstance(scenarios, dict) else None
    if (not isinstance(scenarios, dict)
            or scenarios.get("schema") != "fwrouter-acceptance-scenarios/v1"
            or not isinstance(rows, list)):
        raise gate.GateError("main smoke scenario registry has an unsupported schema")
    expected = acceptance_launcher._DIAGNOSTIC_NODEIDS.get("xray-diagnostic")
    matches = [row for row in rows if isinstance(row, dict) and row.get("nodeid") == expected]
    if not expected or len(matches) != 1 or matches[0].get("suite") != "functional":
        raise gate.GateError("registered native application smoke node is absent or ambiguous")
    return expected


def required_hosted_partitions(rows: list[dict[str, Any]]) -> list[str]:
    profiles = {row.get("execution_profile") for row in rows}
    required: set[str] = set()
    if "hosted-isolated-compose" in profiles:
        required.update({"functional-ordinary", "provider-cohort"})
    if gate.PACKET_EXECUTION_PROFILE in profiles:
        required.add("packet-diagnostic")
    return sorted(required)


def group_hosted_receipts(paths: list[Path], required: list[str], repeats: int
                          ) -> tuple[dict[str, list[Path]], list[str]]:
    grouped = {suite: [] for suite in required}
    problems: list[str] = []
    for path in paths:
        suite = read_json(path).get("suite")
        if suite not in grouped:
            problems.append(f"{path.name}: unexpected profile partition receipt")
            continue
        grouped[suite].append(path)
    for suite, suite_paths in grouped.items():
        if len(suite_paths) != repeats:
            problems.append(f"hosted {suite} receipts: {len(suite_paths)}; required: {repeats}")
    return grouped, problems


def validate_functional_receipt(path: Path, plan: dict[str, Any], expected: set[str],
                                runtime_inputs: dict[str, Any], *, suite: str) -> list[str]:
    receipt = read_json(path)
    expected_profile = {"functional-ordinary": "hosted-native-process",
                        "provider-cohort": "hosted-kernel-dataplane",
                        "packet-diagnostic": "hosted-kernel-packet",
                        "xray-diagnostic": "hosted-native-process"}.get(suite)
    expected_outer_scope = ("hosted-native-diagnostic" if suite == "xray-diagnostic"
                            else expected_profile)
    problems: list[str] = []
    if expected_profile is None:
        return ["hosted receipt suite is not an approved application profile"]
    invalid_diagnostic_flag = (
        (suite == "xray-diagnostic" and receipt.get("diagnostic_only") is not True)
        or (suite == "functional-ordinary" and receipt.get("diagnostic_only") is True)
    )
    if (receipt.get("schema_version") != 1 or receipt.get("scope") != expected_outer_scope
            or invalid_diagnostic_flag):
        problems.append("unsupported receipt schema or execution scope")
    recorded_suite = receipt.get("suite")
    suite_matches = recorded_suite == suite
    if not suite_matches or receipt.get("status") != "passed":
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
        problems.append("JUnit node IDs do not exactly match the selected profile partition")
    if any(tests.get(field) != 0 for field in ("failures", "errors", "skipped")):
        problems.append("JUnit contains failures, errors, or skips")
    if tests.get("tests") != len(expected):
        problems.append("JUnit case count differs from the functional registry")
    inner = receipt.get("application_receipt")
    if not isinstance(inner, dict):
        problems.append("application-owned receipt is absent")
        inner = {}
    if (inner.get("schema") != "fwrouter-application-acceptance-receipt/v2"
            or inner.get("scope") != expected_profile
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
            problems.append("application receipt node IDs do not exactly match the selected profile partition")
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
                or profile.get("profile") != expected_profile
                or profile.get("source_revision") != plan.get("source_commit")
                or profile.get("plan_digest") != plan.get("plan_digest")
                or profile.get("suite_nonce") != nonce):
            problems.append("profile source or plan binding is invalid")
        xray = profile.get("xray") if isinstance(profile.get("xray"), dict) else {}
        mihomo = profile.get("mihomo") if isinstance(profile.get("mihomo"), dict) else {}
        chromium = profile.get("chromium") if isinstance(profile.get("chromium"), dict) else {}
        native = runtime_inputs.get("native") if isinstance(runtime_inputs.get("native"), dict) else {}
        chromium_input = runtime_inputs.get("chromium") if isinstance(runtime_inputs.get("chromium"), dict) else {}
        xray_input = native.get("xray") if isinstance(native.get("xray"), dict) else {}
        mihomo_input = native.get("mihomo") if isinstance(native.get("mihomo"), dict) else {}
        pinned = (
            xray.get("version") == provision.INPUTS["xray"]["version"],
            xray.get("version") == xray_input.get("version"),
            xray.get("sha256") == provision.BINARY_SHA256["xray"]
            and xray.get("sha256") == xray_input.get("binary_sha256"),
            mihomo.get("version") == provision.INPUTS["mihomo"]["version"],
            mihomo.get("version") == mihomo_input.get("version"),
            mihomo.get("sha256") == provision.BINARY_SHA256["mihomo"]
            and mihomo.get("sha256") == mihomo_input.get("binary_sha256"),
            chromium.get("version") == provision.CHROMIUM_VERSION,
            chromium.get("version") == chromium_input.get("version"),
            chromium.get("bundle_sha256") == provision.CHROMIUM_BUNDLE_SHA256
            and chromium.get("bundle_sha256") == chromium_input.get("bundle_sha256"),
            chromium_input.get("source_archive_sha256") == provision.INPUTS["chromium"]["sha256"],
            xray_input.get("source_archive_sha256") == provision.INPUTS["xray"]["sha256"],
            mihomo_input.get("source_archive_sha256") == provision.INPUTS["mihomo"]["sha256"],
            isinstance(chromium.get("sha256"), str)
            and len(chromium.get("sha256", "")) == 64
            and all(char in "0123456789abcdef" for char in chromium.get("sha256", "")),
            profile.get("playwright_python") == provision.PLAYWRIGHT_VERSION,
        )
        if not all(pinned):
            problems.append("native/browser profile does not match exact pinned runtime inputs")
    return problems


def validate_main_smoke_receipt(path: Path, plan: dict[str, Any],
                                runtime_inputs: dict[str, Any], *,
                                hosted_paths: list[Path],
                                required_partitions: list[str]) -> list[str]:
    """Require a strict native application receipt without promoting a partial partition."""
    if path.is_symlink() or not path.is_file():
        return ["NOT RUN: main native application smoke receipt is missing or unsafe"]
    receipt = read_json(path)
    suite = receipt.get("suite")
    expected_node = main_smoke_nodeid()
    if suite == "functional-ordinary":
        if ("functional-ordinary" not in required_partitions
                or path.resolve() not in {item.resolve() for item in hosted_paths}):
            return ["main smoke ordinary receipt is not the selected validated hosted partition"]
        expected = hosted_partition_nodeids()["functional-ordinary"]
    elif suite == "xray-diagnostic":
        expected = {expected_node}
    else:
        return ["main smoke receipt is outside the approved native application scopes"]
    problems = validate_functional_receipt(path, plan, expected, runtime_inputs, suite=suite)
    if problems:
        return [f"main smoke {path.name}: {reason}" for reason in problems]
    inner_rows = receipt.get("application_receipt", {}).get("tests", [])
    observed = {row.get("nodeid") for row in inner_rows if isinstance(row, dict)}
    if expected_node not in observed:
        return ["main smoke receipt does not include the registered native application node"]
    return []


def validate_qcp_receipt(path: Path, plan: dict[str, Any], runtime_inputs: dict[str, Any]) -> tuple[list[str], dict[str, str]]:
    receipt = read_json(path)
    problems: list[str] = []
    tests = receipt.get("tests") if isinstance(receipt.get("tests"), dict) else {}
    statuses = tests.get("node_status") if isinstance(tests.get("node_status"), dict) else {}
    expected_ids = receipt.get("expected_ids")
    if (receipt.get("schema") != "fwrouter-qualified-child-receipt/v1"
            or receipt.get("scope") != "qualified-child-process"
            or receipt.get("status") != "passed"):
        problems.append("qualified-child receipt schema/scope/status did not pass")
    if (receipt.get("source_revision") != plan.get("source_commit")
            or receipt.get("plan_digest") != plan.get("plan_digest")):
        problems.append("qualified-child receipt source or plan does not match")
    nonce = receipt.get("suite_nonce")
    if not isinstance(nonce, str) or len(nonce) != 32 or any(c not in "0123456789abcdef" for c in nonce):
        problems.append("qualified-child receipt nonce is invalid")
    if receipt.get("cleanup") != "owned_resources_removed":
        problems.append("qualified-child owned-resource cleanup is not confirmed")
    if (len(statuses) != 26 or any(value != "passed" for value in statuses.values())
            or tests.get("tests") != 26 or tests.get("failed") != 0 or tests.get("skipped") != 0
            or tests.get("contract_valid") is not True):
        problems.append("qualified-child did not report 26 passing exact node IDs")
        statuses = {}
    if not isinstance(expected_ids, list) or set(expected_ids) != set(statuses) or len(expected_ids) != len(set(expected_ids)):
        problems.append("qualified-child expected ID registry differs from observed node IDs")
    scenario_path = ROOT / "tests/acceptance/qualified-child-scenarios.json"
    scenario = read_json(scenario_path)
    expected_qcp = scenario.get("nodeids", [])
    if (scenario.get("schema") != "fwrouter-qualified-child-scenarios/v1"
            or scenario.get("protocol_case_count") != 20
            or len(expected_qcp) != 26 or len(expected_qcp) != len(set(expected_qcp))
            or set(statuses) != set(expected_qcp)):
        problems.append("qualified-child exact IDs differ from the checked-in 20+6 scenario contract")

    profile_path = path.with_name("profile.json")
    profile = read_json(profile_path)
    profile_hash = hashlib.sha256((json.dumps(profile, sort_keys=True, indent=2) + "\n").encode("utf-8")).hexdigest()
    if (profile_hash != receipt.get("profile_sha256")
            or profile.get("schema") != "fwrouter-qualified-child-profile/v1"
            or profile.get("profile") != "qualified-child-process"
            or profile.get("source_revision") != plan.get("source_commit")
            or profile.get("plan_digest") != plan.get("plan_digest")
            or profile.get("suite_nonce") != nonce):
        problems.append("qualified-child archived profile is not bound to source/plan/run")
    mihomo = profile.get("mihomo") if isinstance(profile.get("mihomo"), dict) else {}
    qcp_mihomo = runtime_inputs.get("native", {}).get("mihomo", {})
    if (mihomo.get("version") != provision.INPUTS["mihomo"]["version"]
            or mihomo.get("sha256") != provision.BINARY_SHA256["mihomo"]
            or mihomo.get("sha256") != qcp_mihomo.get("binary_sha256")):
        problems.append("qualified-child Mihomo does not match the pinned hosted binary")
    return problems, statuses


def validate_xray_docker_receipt(path: Path, plan: dict[str, Any],
                                 runtime_inputs: dict[str, Any]) -> tuple[list[str], dict[str, str]]:
    receipt = read_json(path)
    problems: list[str] = []
    nodeid = "backend/tests/test_xray_native_readback.py::test_loaded_client_readback_against_isolated_xray_26_2_6"
    tests = receipt.get("tests") if isinstance(receipt.get("tests"), dict) else {}
    statuses = tests.get("node_status") if isinstance(tests.get("node_status"), dict) else {}
    nonce = receipt.get("suite_nonce")
    if (receipt.get("schema") != "fwrouter-qualified-docker-xray-receipt/v1"
            or receipt.get("scope") != "qualified-docker-xray"
            or receipt.get("status") != "passed"):
        problems.append("qualified Docker-Xray receipt schema/scope/status did not pass")
    if (receipt.get("source_revision") != plan.get("source_commit")
            or receipt.get("plan_digest") != plan.get("plan_digest")):
        problems.append("qualified Docker-Xray source or plan does not match")
    if not isinstance(nonce, str) or len(nonce) != 32 or any(c not in "0123456789abcdef" for c in nonce):
        problems.append("qualified Docker-Xray run nonce is invalid")
    if (receipt.get("cleanup") != "owned_resources_removed"
            or receipt.get("expected_ids") != [nodeid]
            or statuses != {nodeid: "passed"}
            or tests.get("passed") != 1 or tests.get("failed") != 0
            or tests.get("skipped") != 0 or tests.get("contract_valid") is not True):
        problems.append("qualified Docker-Xray did not pass the one exact native readback node")
    profile = read_json(path.with_name("profile.json"))
    profile_hash = hashlib.sha256((json.dumps(profile, sort_keys=True, indent=2) + "\n").encode("utf-8")).hexdigest()
    if (profile_hash != receipt.get("profile_sha256")
            or profile.get("schema") != "fwrouter-qualified-docker-xray-profile/v1"
            or profile.get("profile") != "qualified-docker-xray"
            or profile.get("source_revision") != plan.get("source_commit")
            or profile.get("plan_digest") != plan.get("plan_digest")
            or profile.get("suite_nonce") != nonce
            or profile.get("image_id") != receipt.get("image_id")):
        problems.append("qualified Docker-Xray archived profile is not bound to this source/plan/image/run")
    xray_runtime = runtime_inputs.get("native", {}).get("xray", {})
    if (receipt.get("xray_binary_sha256") != provision.BINARY_SHA256["xray"]
            or profile.get("xray_binary_sha256") != provision.BINARY_SHA256["xray"]
            or receipt.get("xray_binary_sha256") != xray_runtime.get("binary_sha256")
            or profile.get("xray_version") != provision.INPUTS["xray"]["version"]):
        problems.append("qualified Docker-Xray binary does not match the verified hosted pin")
    if not isinstance(receipt.get("image_id"), str) or not receipt["image_id"].startswith("sha256:"):
        problems.append("qualified Docker-Xray immutable image ID is missing")
    return problems, (statuses if not problems else {})


def aggregate(plan_path: Path, unit_path: Path, hosted_paths: list[Path], qcp_paths: list[Path],
              xray_docker_paths: list[Path], *, minimum_repeats: int,
              runtime_inputs_path: Path | None = None,
              require_main_smoke: bool = False,
              main_smoke_path: Path | None = None) -> dict[str, Any]:
    manifest = gate.load_manifest()
    plan = read_json(plan_path)
    gate.ensure_plan(plan, manifest, allow_l6=bool(plan.get("full_suite_required")))
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
    xray_docker_path = "backend/tests/test_xray_native_readback.py"
    qualified_child = sorted(path for path in deferred
                             if rows[path].get("execution_profile") == "qualified-child-process"
                             and path != xray_docker_path)
    required_xray_docker = sorted(path for path in deferred if path == xray_docker_path)
    required_hosted = sorted(path for path in deferred
                             if rows[path].get("execution_profile") == "hosted-isolated-compose")
    required_partitions = required_hosted_partitions([rows[path] for path in selected])
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
    partition_ids = hosted_partition_nodeids()
    hosted_statuses: dict[str, str] = {}
    receipts: list[dict[str, Any]] = []
    verified_run_nonces: list[str] = []
    runtime_inputs: dict[str, Any] = {}
    if required_partitions:
        if runtime_inputs_path is None:
            raise gate.GateError("hosted runtime input manifest is required to verify extracted binary hashes")
        runtime_inputs = read_json(runtime_inputs_path)
        validate_runtime_inputs(runtime_inputs)
        receipts_by_suite, grouping_problems = group_hosted_receipts(
            hosted_paths, required_partitions, minimum_repeats)
        problems.extend(grouping_problems)
        for suite in required_partitions:
            paths = receipts_by_suite[suite]
            for path in paths:
                receipt = read_json(path)
                receipt_problems = validate_functional_receipt(
                    path, plan, partition_ids[suite], runtime_inputs, suite=suite)
                problems.extend(f"hosted {path.name}: {reason}" for reason in receipt_problems)
                if not receipt_problems:
                    receipts.append(receipt)
                    verified_run_nonces.append(receipt["suite_nonce"])
                    for nodeid in partition_ids[suite]:
                        previous = hosted_statuses.get(nodeid)
                        if previous is not None and previous != "passed":
                            problems.append(f"hosted repetitions disagree for {nodeid}")
                        hosted_statuses[nodeid] = "passed"
        nonces = [receipt.get("suite_nonce") for receipt in receipts]
        if len(set(nonces)) != len(nonces):
            problems.append("hosted profile runs do not have distinct suite nonces")
    elif hosted_paths:
        problems.append("unexpected hosted receipt supplied when no hosted profile is selected")

    qcp_statuses: dict[str, str] = {}
    qcp_receipt_verified = False
    if qualified_child:
        if runtime_inputs_path is None:
            raise gate.GateError("hosted runtime input manifest is required for QCP receipts")
        if not runtime_inputs:
            runtime_inputs = read_json(runtime_inputs_path)
            validate_runtime_inputs(runtime_inputs)
        if not qcp_paths:
            problems.append("NOT RUN: qualified-child receipt is missing for: " + ", ".join(qualified_child))
        elif len(qcp_paths) != 1:
            problems.append("qualified-child profile requires exactly one isolated receipt")
        qcp_nonces: set[str] = set()
        for path in qcp_paths:
            qcp_problems, statuses = validate_qcp_receipt(path, plan, runtime_inputs)
            problems.extend(f"QCP {path.name}: {reason}" for reason in qcp_problems)
            if qcp_problems:
                statuses = {}
            else:
                qcp_receipt_verified = True
            nonce = read_json(path).get("suite_nonce")
            if nonce in qcp_nonces:
                problems.append("qualified-child receipts repeat the same run nonce")
            if isinstance(nonce, str):
                qcp_nonces.add(nonce)
                if not qcp_problems:
                    verified_run_nonces.append(nonce)
            for nodeid, status in statuses.items():
                if nodeid in qcp_statuses and qcp_statuses[nodeid] != status:
                    problems.append(f"qualified-child repetitions disagree for {nodeid}")
                qcp_statuses[nodeid] = status
    elif qcp_paths:
        problems.append("unexpected qualified-child receipt supplied when no QCP profile is selected")

    xray_docker_statuses: dict[str, str] = {}
    if required_xray_docker:
        if runtime_inputs_path is None:
            raise gate.GateError("hosted runtime input manifest is required for Docker-Xray receipt")
        if not runtime_inputs:
            runtime_inputs = read_json(runtime_inputs_path)
            validate_runtime_inputs(runtime_inputs)
        if len(xray_docker_paths) != 1:
            problems.append("NOT RUN: exactly one qualified Docker-Xray receipt is required")
        for path in xray_docker_paths:
            xray_problems, statuses = validate_xray_docker_receipt(path, plan, runtime_inputs)
            problems.extend(f"Docker-Xray {path.name}: {reason}" for reason in xray_problems)
            if not xray_problems:
                xray_docker_statuses.update(statuses)
                verified_run_nonces.append(read_json(path)["suite_nonce"])
    elif xray_docker_paths:
        problems.append("unexpected Docker-Xray receipt supplied when no exact Docker-Xray suite is selected")

    main_smoke_statuses: dict[str, str] = {}
    if require_main_smoke:
        if main_smoke_path is None:
            problems.append("NOT RUN: required main native application smoke report was not supplied")
        else:
            try:
                if not runtime_inputs:
                    if runtime_inputs_path is None:
                        raise gate.GateError("main smoke requires the pinned hosted runtime input manifest")
                    runtime_inputs = read_json(runtime_inputs_path)
                    validate_runtime_inputs(runtime_inputs)
                smoke_problems = validate_main_smoke_receipt(
                    main_smoke_path, plan, runtime_inputs, hosted_paths=hosted_paths,
                    required_partitions=required_partitions)
                problems.extend(smoke_problems)
                if not smoke_problems:
                    main_smoke_statuses[main_smoke_nodeid()] = "passed"
                    smoke_receipt = read_json(main_smoke_path)
                    if smoke_receipt.get("suite") == "xray-diagnostic":
                        verified_run_nonces.append(smoke_receipt["suite_nonce"])
            except (gate.GateError, OSError, ValueError, KeyError, json.JSONDecodeError) as exc:
                problems.append("main smoke: " + gate.redact_failure_output(str(exc))[:512])
    elif main_smoke_path is not None:
        problems.append("unexpected main smoke report supplied without --require-main-smoke")

    if not run_nonces_are_distinct(verified_run_nonces):
        problems.append("verified hosted and isolated receipts do not have distinct run nonces")

    merged_status = dict(unit.get("node_status", {}))
    for nodeid, status in hosted_statuses.items():
        if nodeid in merged_status and merged_status[nodeid] != status:
            problems.append(f"hosted pass conflicts with unit status for {nodeid}")
        merged_status[nodeid] = status
    for nodeid, status in qcp_statuses.items():
        if nodeid in merged_status and merged_status[nodeid] != status:
            problems.append(f"qualified-child pass conflicts with unit status for {nodeid}")
        merged_status[nodeid] = status
    for nodeid, status in xray_docker_statuses.items():
        if nodeid in merged_status and merged_status[nodeid] != status:
            problems.append(f"Docker-Xray pass conflicts with unit status for {nodeid}")
        merged_status[nodeid] = status
    for nodeid, status in main_smoke_statuses.items():
        if nodeid in merged_status and merged_status[nodeid] != status:
            problems.append(f"main smoke pass conflicts with unit status for {nodeid}")
        merged_status[nodeid] = status
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
    if "packet-diagnostic" in required_partitions:
        completed_levels.add(rows[gate.PACKET_EXECUTION_PATH]["primary_level"])
    if qcp_statuses:
        for path in qualified_child:
            completed_levels.add(rows[path]["primary_level"])
    if required_xray_docker and xray_docker_paths:
        completed_levels.add(rows[xray_docker_path]["primary_level"])
    if main_smoke_statuses:
        smoke_path = main_smoke_nodeid().split("::", 1)[0]
        if smoke_path in rows:
            completed_levels.add(rows[smoke_path]["primary_level"])
    missing_levels = sorted(set(plan.get("required_levels", [])) - completed_levels)
    if missing_levels:
        problems.append("required levels are incomplete: " + ", ".join(missing_levels))
    if deferred and not required_partitions and not qualified_child:
        problems.append("profile selection has no matching hosted receipt adapter")
    return {
        "schema_version": 1,
        "source_commit": plan["source_commit"],
        "plan_digest": plan["plan_digest"],
        "manifest_version": manifest["version"],
        "selected_files": sorted(selected),
        "deferred_profile_files": sorted(deferred),
        "qualified_child_process_selected": qualified_child,
        "qualified_child_process_not_run": [] if qcp_receipt_verified else qualified_child,
        "qualified_child_receipt_count": len(qcp_paths),
        "qualified_xray_docker_receipt_count": len(xray_docker_paths),
        "main_smoke_required": require_main_smoke,
        "main_smoke_status": "passed" if main_smoke_statuses else ("not-run" if require_main_smoke else "not-required"),
        "hosted_receipt_count": len(receipts),
        "hosted_required_partitions": required_partitions,
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
    parser.add_argument("--qcp-report", action="append", default=[], type=Path)
    parser.add_argument("--xray-docker-report", action="append", default=[], type=Path)
    parser.add_argument("--minimum-repeats", type=int, default=1, choices=(1, 2))
    parser.add_argument("--runtime-inputs", type=Path)
    parser.add_argument("--require-main-smoke", action="store_true")
    parser.add_argument("--main-smoke-report", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--unit-failure-report", action="store_true",
                        help="write a bounded non-promoting report after the routine unit gate fails")
    parser.add_argument("--unit-exit-code", type=int,
                        help="nonzero routine unit process status for --unit-failure-report")
    args = parser.parse_args()
    try:
        if args.unit_failure_report:
            if args.unit_exit_code is None:
                raise gate.GateError("--unit-failure-report requires --unit-exit-code")
            result = unit_failure_report(args.plan, args.unit_report, args.unit_exit_code)
            if args.output:
                gate.atomic_json(args.output, result)
            print(json.dumps(result, sort_keys=True, indent=2))
            return 1
        result = aggregate(args.plan, args.unit_report, args.hosted_report, args.qcp_report,
                           args.xray_docker_report,
                           minimum_repeats=args.minimum_repeats,
                           runtime_inputs_path=args.runtime_inputs,
                           require_main_smoke=args.require_main_smoke,
                           main_smoke_path=args.main_smoke_report)
        if args.output:
            gate.atomic_json(args.output, result)
        print(json.dumps(result, sort_keys=True, indent=2))
        return 0 if result["eligible"] else 1
    except (gate.GateError, OSError, ValueError, KeyError, json.JSONDecodeError) as exc:
        print(f"aggregate: {gate.redact_failure_output(str(exc))[:1024]}", file=sys.stderr)
        if args.output:
            try:
                gate.atomic_json(args.output, aggregate_error_report(exc))
            except (OSError, ValueError) as write_exc:
                print(f"aggregate: could not persist bounded error report: {write_exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
