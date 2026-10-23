#!/usr/bin/env python3
"""Manifest-owned routine shard planning, receipt validation and aggregation."""
from __future__ import annotations

import argparse
import html
import json
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
import gate


def selected_rows(plan: dict[str, Any], manifest: dict[str, Any]) -> list[dict[str, Any]]:
    selected = set(plan.get("selected_files", []))
    rows = [row for row in manifest.get("test_files", []) if row["path"] in selected]
    if len(rows) != len(selected):
        raise gate.GateError("routine shard plan contains unclassified or duplicate selected paths")
    return rows


def validate_l5_coverage(plan: dict[str, Any], manifest: dict[str, Any],
                         executions: list[dict[str, Any]]) -> dict[str, Any]:
    """Prove every plan-required L5 anchor ran once under its primary owner."""
    policy = plan.get("regression_policy")
    if plan.get("full_suite_required") is True:
        if not isinstance(policy, dict) or policy.get("level") != "L6":
            raise gate.GateError("L6 full plan lacks its regression policy")
        policy = {"level": "L5", "anchors": sorted(manifest["policies"]["L5"]["anchors"]),
                  "reasons": sorted(set(plan.get("domains", [])) & set(manifest["policies"]["L5"]["shared_domains"]))}
    if not isinstance(policy, dict) or policy.get("level") != "L5":
        raise gate.GateError("routine aggregate lacks the immutable L5 regression policy")
    canonical = sorted(manifest.get("policies", {}).get("L5", {}).get("anchors", []))
    shared_domains = set(manifest.get("policies", {}).get("L5", {}).get("shared_domains", []))
    reasons = sorted(set(plan.get("domains", [])) & shared_domains)
    expected = canonical if plan.get("mandatory_suite_required") or plan.get("full_suite_required") or reasons else []
    anchors = policy.get("anchors")
    if (policy.get("reasons") != reasons or anchors != expected
            or len(anchors) != len(set(anchors))):
        raise gate.GateError("plan L5 anchors/reasons differ from manifest-derived policy")
    selected = {row["path"]: row for row in selected_rows(plan, manifest)}
    if not set(expected) <= set(selected):
        raise gate.GateError("required L5 anchor is absent from the immutable selected-file plan")
    by_path: dict[str, list[dict[str, Any]]] = {}
    for row in executions:
        by_path.setdefault(row.get("path", ""), []).append(row)
    evidence = []
    for path in expected:
        row = selected[path]
        matching = by_path.get(path, [])
        if len(matching) != 1:
            raise gate.GateError(f"L5 anchor execution is missing or duplicated: {path}")
        execution = matching[0]
        nodes = execution.get("collected_node_ids")
        statuses = execution.get("node_status")
        if (execution.get("status") != "passed" or execution.get("execution_profile")
                or not isinstance(nodes, list) or not nodes or not isinstance(statuses, dict)
                or any(statuses.get(node) != "passed" for node in nodes)):
            raise gate.GateError(f"L5 anchor lacks completed primary-owner test evidence: {path}")
        evidence.append({"path": path, "suite": row["id"], "primary_level": row["primary_level"],
                         "domain": row["domain"], "execution_group": row["execution_group"],
                         "status": "passed", "node_ids": nodes})
    return {"status": "passed", "required_count": len(expected), "anchors": evidence}


def expected_by_group(plan: dict[str, Any], manifest: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    problems = gate.check_catalog(manifest)
    if problems:
        raise gate.GateError("routine shard manifest ownership is invalid: " + "; ".join(problems[:8]))
    grouped = {name: [] for name in gate.ROUTINE_SHARD_GROUPS}
    for row in selected_rows(plan, manifest):
        group = row.get("execution_group")
        if group not in grouped:
            raise gate.GateError(f"selected suite lacks a supported shard owner: {row['path']}")
        grouped[group].append(row)
    for rows in grouped.values():
        rows.sort(key=lambda row: row["path"])
    flattened = [row["path"] for rows in grouped.values() for row in rows]
    if len(flattened) != len(set(flattened)) or set(flattened) != set(plan["selected_files"]):
        raise gate.GateError("routine shard owners do not form a disjoint exhaustive selected-file partition")
    return grouped


def verify_shard(report: dict[str, Any], group: str, plan: dict[str, Any],
                 rows: list[dict[str, Any]], *, run_id: str, attempt: str) -> None:
    digest = report.get("shard_digest")
    unsigned = dict(report)
    unsigned.pop("shard_digest", None)
    if not isinstance(digest, str) or digest != gate.canonical_digest(unsigned):
        raise gate.GateError(f"{group}: shard digest is missing or invalid")
    expected_fields = {
        "schema": "fwrouter-routine-shard/v1", "source_commit": plan["source_commit"],
        "plan_digest": plan["plan_digest"], "manifest_digest": plan["manifest_digest"],
        "manifest_version": plan["manifest_version"], "shard_group": group,
        "run_id": run_id, "run_attempt": attempt, "status": "passed",
        "qualification_scope": "shard-only; full eligibility requires strict aggregate",
    }
    if any(report.get(key) != value for key, value in expected_fields.items()):
        raise gate.GateError(f"{group}: shard source, owner, run/attempt or status does not match")
    expected = [{"suite": row["id"], "path": row["path"],
                 "primary_level": row["primary_level"], "domain": row["domain"],
                 "execution_group": group,
                 "execution_profile": row.get("execution_profile")}
                for row in rows]
    paths = [row["path"] for row in expected]
    if (report.get("expected_suites") != expected
            or report.get("expected_suite_ids") != [row["suite"] for row in expected]
            or report.get("expected_paths") != paths):
        raise gate.GateError(f"{group}: shard expected-file ownership differs from the immutable manifest")
    executions = report.get("executions")
    if not isinstance(executions, list):
        raise gate.GateError(f"{group}: execution receipt is malformed")
    observed_paths = [row.get("path") for row in executions if isinstance(row, dict)]
    if (len(observed_paths) != len(executions) or len(observed_paths) != len(set(observed_paths))
            or observed_paths != paths):
        raise gate.GateError(f"{group}: missing, duplicate, unexpected or reordered suite executions")
    expected_map = {row["path"]: row for row in expected}
    deferred = []
    executed = []
    for execution in executions:
        item = expected_map[execution["path"]]
        if (execution.get("suite") != item["suite"]
                or execution.get("primary_level") != item["primary_level"]
                or execution.get("domain") != item["domain"]
                or execution.get("execution_group") != group
                or execution.get("execution_profile") != item["execution_profile"]):
            raise gate.GateError(f"{group}: suite ID or level disagrees with the manifest")
        if item["execution_profile"]:
            if execution.get("status") != "deferred_remote_hosted":
                raise gate.GateError(f"{group}: remote-profile suite did not remain explicitly deferred")
            deferred.append(execution["path"])
        else:
            if execution.get("status") != "passed":
                raise gate.GateError(f"{group}: routine suite is not a complete PASS")
            executed.append(execution["path"])
    if (report.get("collected_paths") != observed_paths
            or report.get("collected_suite_ids") != [row["suite"] for row in executions]
            or report.get("executed_paths") != executed
            or report.get("executed_suite_ids") != [row["suite"] for row in executions
                                                       if row.get("status") == "passed"]
            or report.get("blocked_reasons") != []):
        raise gate.GateError(f"{group}: shard collected/executed inventory is inconsistent")
    statuses = report.get("node_status")
    ids = report.get("collected_node_ids")
    expected_nodes = report.get("expected_node_ids")
    executed_nodes = report.get("executed_node_ids")
    if (not isinstance(statuses, dict) or not all(isinstance(k, str) and isinstance(v, str)
                                                 and v in {"passed", "failed", "skipped"}
                                                 for k, v in statuses.items())
            or not isinstance(ids, list) or ids != sorted(statuses)
            or len(ids) != len(set(ids))
            or expected_nodes != ids or executed_nodes != ids
            or report.get("node_status_count") != len(statuses)
            or report.get("unique_node_status_count") != len(set(statuses))):
        raise gate.GateError(f"{group}: node-ID collection is missing, duplicated or malformed")
    row_node_ids = [node for execution in executions
                    for node in execution.get("collected_node_ids", [])]
    row_executed_ids = [node for execution in executions
                        for node in execution.get("executed_node_ids", [])]
    if (row_node_ids != ids or row_executed_ids != executed_nodes
            or len(row_node_ids) != len(set(row_node_ids))):
        raise gate.GateError(f"{group}: per-suite node IDs are missing, duplicated or inconsistent")
    for execution in executions:
        per_suite = execution.get("node_status")
        per_suite_ids = execution.get("collected_node_ids")
        if (not isinstance(per_suite, dict) or not isinstance(per_suite_ids, list)
                or per_suite_ids != sorted(per_suite)
                or any(statuses.get(node) != value for node, value in per_suite.items())):
            raise gate.GateError(f"{group}: per-suite node status disagrees with the shard inventory")
        if any(node.split("::", 1)[0] != execution["path"] for node in per_suite_ids):
            raise gate.GateError(f"{group}: collected node ID is attributed to the wrong suite path")
        if execution.get("status") == "deferred_remote_hosted":
            local_overrides = sorted(
                f"{execution['path']}::{override['node']}"
                for override in gate.load_manifest().get("node_overrides", [])
                if override.get("path") == execution["path"]
                and "execution_profile" in override and override.get("execution_profile") is None
            )
            if (per_suite_ids != local_overrides
                    or (local_overrides and execution.get("routine_node_status") != per_suite)):
                raise gate.GateError(f"{group}: profiled suite routine carve-outs are missing or inconsistent")
        elif not per_suite_ids:
            raise gate.GateError(f"{group}: executed routine suite has no observed test IDs")
    if any(status != "passed" for status in statuses.values()):
        raise gate.GateError(f"{group}: node evidence contains a failure or skip")
    file_markers = report.get("file_marker_paths")
    if not isinstance(file_markers, list) or len(file_markers) != len(set(file_markers)):
        raise gate.GateError(f"{group}: file-marker inventory is malformed")
    for marker in file_markers:
        if marker not in statuses or marker.split("::", 1)[0] not in gate.STDLIB_CONTRACT_PATHS:
            raise gate.GateError(f"{group}: file marker claims a non-stdlib test ID")
    owned_l0 = group == gate.ROUTINE_SHARD_GROUPS[0]
    l0 = report.get("l0")
    if (not isinstance(l0, dict)
            or l0.get("owned") is not owned_l0
            or l0.get("status") != ("passed" if owned_l0 else "not_owned")
            or (owned_l0 and (not isinstance(l0.get("elapsed_seconds"), (int, float))
                              or isinstance(l0.get("elapsed_seconds"), bool)))):
        raise gate.GateError(f"{group}: L0 ownership or result is invalid")
    resources_before = report.get("resources_before")
    resources_after = report.get("resources_after")
    required_resource_keys = {"cpu_count", "memory", "disk_total_bytes", "disk_used_bytes", "disk_free_bytes"}
    cleanup_receipt = report.get("owned_temp_cleanup")
    expected_root_prefix = f"fwrouter-gate-{plan['source_commit'][:8]}-"
    cleanup_root = cleanup_receipt.get("root_name") if isinstance(cleanup_receipt, dict) else None
    cleanup_suffix = (cleanup_root[len(expected_root_prefix):]
                      if isinstance(cleanup_root, str) and cleanup_root.startswith(expected_root_prefix)
                      else "")
    if (report.get("cleanup") != "verified_owned_gate_temp_removed"
            or not isinstance(cleanup_receipt, dict)
            or cleanup_receipt.get("status") != "removed"
            or not isinstance(cleanup_root, str)
            or not cleanup_root.startswith(expected_root_prefix)
            or not 1 <= len(cleanup_suffix) <= 64
            or not all(char.isalnum() or char in "_-" for char in cleanup_suffix)
            or cleanup_receipt.get("entries") != []
            or cleanup_receipt.get("truncated") is not False
            or not isinstance(cleanup_receipt.get("readonly_directories_restored"), int)
            or isinstance(cleanup_receipt.get("readonly_directories_restored"), bool)
            or cleanup_receipt["readonly_directories_restored"] < 0
            or cleanup_receipt.get("diagnostic_errors")
            or cleanup_receipt.get("cleanup_error")
            or not isinstance(report.get("duration_seconds"), (int, float))
            or isinstance(report.get("duration_seconds"), bool)
            or report["duration_seconds"] < 0
            or not isinstance(resources_before, dict) or not required_resource_keys <= set(resources_before)
            or not isinstance(resources_after, dict) or not required_resource_keys <= set(resources_after)):
        raise gate.GateError(f"{group}: runner resource or cleanup evidence is incomplete")
    peak = report.get("process_peak")
    if (not isinstance(peak, dict) or not isinstance(peak.get("max_rss_kib"), int)
            or isinstance(peak.get("max_rss_kib"), bool) or peak["max_rss_kib"] <= 0):
        raise gate.GateError(f"{group}: process peak resource evidence is incomplete")


def merge_reports(plan: dict[str, Any], manifest: dict[str, Any], reports: dict[str, dict[str, Any]],
                  *, run_id: str, attempt: str) -> dict[str, Any]:
    grouped = expected_by_group(plan, manifest)
    if set(reports) != set(gate.ROUTINE_SHARD_GROUPS):
        missing = sorted(set(gate.ROUTINE_SHARD_GROUPS) - set(reports))
        extra = sorted(set(reports) - set(gate.ROUTINE_SHARD_GROUPS))
        raise gate.GateError(f"routine shard report set is incomplete or unexpected; missing={missing}, extra={extra}")
    node_status: dict[str, str] = {}
    file_markers: list[str] = []
    rows_by_path: dict[str, dict[str, Any]] = {}
    shard_summaries = []
    l0_report = None
    for group in gate.ROUTINE_SHARD_GROUPS:
        report = reports[group]
        verify_shard(report, group, plan, grouped[group], run_id=run_id, attempt=attempt)
        for row in report["executions"]:
            if row["path"] in rows_by_path:
                raise gate.GateError(f"duplicate execution owner for {row['path']}")
            rows_by_path[row["path"]] = row
        overlap = set(node_status) & set(report["node_status"])
        if overlap:
            raise gate.GateError("duplicate collected node IDs across shards: " + ", ".join(sorted(overlap)[:8]))
        node_status.update(report["node_status"])
        file_markers.extend(report["file_marker_paths"])
        if report["l0"]["owned"]:
            if l0_report is not None:
                raise gate.GateError("L0 has multiple shard owners")
            l0_report = report["l0"]
        shard_summaries.append({
            "shard_group": group, "shard_digest": report["shard_digest"],
            "expected_file_count": len(report["expected_paths"]),
            "collected_file_count": len(report["collected_paths"]),
            "executed_file_count": len(report["executed_paths"]),
            "collected_node_count": len(report["collected_node_ids"]),
            "duration_seconds": report["duration_seconds"],
            "selected_levels": report["selected_levels"],
            "selected_domains": report["selected_domains"],
            "cleanup": report.get("cleanup"),
        })
    if l0_report is None:
        raise gate.GateError("routine shard set does not include the unique L0 owner")
    selected = selected_rows(plan, manifest)
    if set(rows_by_path) != set(plan["selected_files"]):
        raise gate.GateError("merged routine shard coverage is not exhaustive")
    executions = []
    node_ownership = []
    for row in selected:
        execution = dict(rows_by_path[row["path"]])
        execution["execution_group"] = row["execution_group"]
        execution["domain"] = row["domain"]
        execution["execution_profile"] = row.get("execution_profile")
        executions.append(execution)
        for node in execution.get("collected_node_ids", []):
            node_ownership.append({
                "node_id": node, "suite": row["id"], "path": row["path"],
                "primary_level": row["primary_level"], "domain": row["domain"],
                "execution_group": row["execution_group"],
                "execution_profile": row.get("execution_profile"),
                "status": execution.get("node_status", {}).get(node, "missing"),
            })
    deferred = sorted(row["path"] for row in selected if row.get("execution_profile"))
    passed_levels = {row["primary_level"] for row in selected
                     if rows_by_path[row["path"]].get("status") == "passed"}
    report = {
        "schema_version": 1,
        "evidence_scope": ("L6_full_suite" if plan.get("full_suite_required") else
                           "mandatory_all_safe_suites" if plan.get("mandatory_suite_required") else
                           "affected_change"),
        "deploy_eligibility": False if plan.get("manual_subset") else None,
        "source_commit": plan["source_commit"], "baseline_commit": None,
        "manifest_version": manifest["version"], "manifest_digest": plan["manifest_digest"],
        "tool_version": gate.TOOL_VERSION,
        "required_execution_profiles": plan["required_execution_profiles"],
        "quick_deferred_profile_suites": plan.get("quick_deferred_profile_suites", []),
        "deferred_remote_suites": deferred,
        "execution_complete": not deferred,
        "available_execution_profiles": [],
        "plan_digest": plan["plan_digest"],
        "completed_levels": sorted({"L0", *passed_levels}),
        "l0": {"status": "passed", "elapsed_seconds": l0_report["elapsed_seconds"],
               "checks": l0_report["checks"], "problems": []},
        "executions": [{"suite": "L0-static", "path": "@L0", "status": "passed",
                        "primary_level": "L0", "elapsed_seconds": l0_report["elapsed_seconds"]}, *executions],
        "node_status": node_status,
        "file_marker_paths": sorted(file_markers),
        "node_ownership": node_ownership,
        "blocked_reasons": [], "failure_diagnostics": [],
        "raw_output_retained": False,
        "redacted_failure_output_retained": False,
        "routine_shards": shard_summaries,
        "eligible": False,
        "eligibility_note": "routine shards are not a qualification result; strict aggregate validates hosted/native evidence",
    }
    report["l5_coverage"] = validate_l5_coverage(plan, manifest, executions)
    report["report_digest"] = gate.canonical_digest(report)
    return report


def write_summary(report: dict[str, Any]) -> str:
    group = report["shard_group"]
    expected = report["expected_suites"]
    executions = {row["path"]: row for row in report["executions"]}
    rows = [
        "## FWRouter CI routine shard: " + group,
        "",
        f"**Shard evidence:** {report['status']} (not full qualification)  ",
        f"Expected files: {len(expected)}; collected files: {len(report['collected_paths'])}; "
        f"executed locally: {len(report['executed_paths'])}; deferred to profile jobs: "
        f"{sum(1 for row in expected if row['execution_profile'])}; expected/collected/executed node IDs: "
        f"{len(report['expected_node_ids'])}/{len(report['collected_node_ids'])}/"
        f"{len(report['executed_node_ids'])}; unique node IDs: {report['unique_node_status_count']}; "
        f"elapsed: {report['duration_seconds']:.3f}s.",
        "",
        "| Test suite | Level | Domain | Profile | Status | Seconds | Collected IDs |",
        "|---|---:|---|---|---|---:|---:|",
    ]
    for item in expected:
        execution = executions.get(item["path"], {})
        node_count = sum(1 for node in report["node_status"] if node.split("::", 1)[0] == item["path"])
        suite_label = html.escape(str(item["suite"])).replace("|", "&#124;")
        rows.append(f"| `{suite_label}` | {item['primary_level']} | {item['domain']} | "
                    f"{item['execution_profile'] or 'routine'} | {execution.get('status', 'missing')} | "
                    f"{float(execution.get('elapsed_seconds', 0.0)):.3f} | {node_count} |")
        if execution.get("collected_node_ids"):
            details = {node: report["node_status"].get(node, "missing")
                       for node in execution["collected_node_ids"]}
            rows.append(f"| └ IDs | | | | <details><summary>{len(details)} observed</summary><pre>"
                        f"{html.escape(json.dumps(details, ensure_ascii=False, sort_keys=True))}"
                        "</pre></details> | | |")
    rows.extend(["", "### Runner resources", "", "| Snapshot | CPU | RAM available | Disk free |", "|---|---:|---:|---:|"])
    for label in ("before", "after"):
        resource = report.get(f"resources_{label}", {})
        memory = resource.get("memory", {})
        rows.append(f"| {label} | {resource.get('cpu_count', 'unknown')} | "
                    f"{memory.get('MemAvailable_bytes', 'unknown')} bytes | "
                    f"{resource.get('disk_free_bytes', 'unknown')} bytes |")
    rows.extend(["", f"Cleanup: `{report.get('cleanup', 'unknown')}`.",
                 f"Process peak RSS: `{report.get('process_peak', {}).get('max_rss_kib', 'unknown')}` KiB.",
                 "Collected IDs come from same-runner collection/execution receipts. Shell installer smoke is one explicit file-level case, not a method count."])
    return "\n".join(rows) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("merge", "summary"))
    parser.add_argument("--plan", type=Path)
    parser.add_argument("--reports", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    try:
        if args.command == "summary":
            report = json.loads(args.report.read_text(encoding="utf-8"))
            summary = write_summary(report)
            if args.output:
                args.output.write_text(summary, encoding="utf-8")
            else:
                print(summary, end="")
            return 0
        plan = json.loads(args.plan.read_text(encoding="utf-8"))
        manifest = gate.load_manifest()
        reports = {group: json.loads((args.reports / group / "shard.json").read_text(encoding="utf-8"))
                   for group in gate.ROUTINE_SHARD_GROUPS}
        merged = merge_reports(plan, manifest, reports,
                               run_id=str(__import__("os").environ["GITHUB_RUN_ID"]),
                               attempt=str(__import__("os").environ["GITHUB_RUN_ATTEMPT"]))
        gate.atomic_json(args.output, merged)
        return 0
    except (OSError, ValueError, KeyError, json.JSONDecodeError, gate.GateError) as exc:
        print(f"routine-shards: {gate.redact_failure_output(str(exc))[:1024]}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
