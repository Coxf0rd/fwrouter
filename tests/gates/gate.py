#!/usr/bin/env python3
"""Deterministic, fail-closed FWRouter test gate planner and reporter.

This file deliberately uses only the Python standard library.  Test commands are
executed serially and always receive explicit time and artifact bounds.
"""
from __future__ import annotations

import argparse
import fnmatch
import hashlib
import json
import os
import re
import re
import shlex
import subprocess
import sys
import tempfile
import threading
import queue
import signal
import shutil
import time
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
GATES = ROOT / "tests" / "gates"
MANIFEST = GATES / "manifest.json"
REPORT_DIR = ROOT / "knowledge" / "audits" / "test_architecture_cicd_2026-10-04" / "reports"
SCHEMA_VERSION = 1
TOOL_VERSION = "fwrouter-test-gate/3"
MAX_REPORT_BYTES = 8 * 1024 * 1024
LEVELS = {f"L{i}" for i in range(8)}
TEST_FILE_GLOBS = (
    "backend/tests/test_*.py",
    "ui/tests/*.test.js",
    "installer/tests/test_*.py",
    "installer/test-install.sh",
    "integrations/homeassistant/tests/test_*.py",
    "tests/application_acceptance/test_*.py",
    "tests/gates/test_*.py",
)


class GateError(RuntimeError):
    pass


def canonical_digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def load_manifest(path: Path = MANIFEST) -> dict[str, Any]:
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise GateError(f"cannot read manifest {path}: {exc}") from exc
    validate_manifest(manifest)
    return manifest


def validate_manifest(manifest: dict[str, Any]) -> None:
    if manifest.get("schema_version") != SCHEMA_VERSION:
        raise GateError("unsupported or missing manifest schema_version")
    if not isinstance(manifest.get("version"), str) or not manifest["version"]:
        raise GateError("manifest version must be a nonempty string")
    rows = manifest.get("test_files")
    if not isinstance(rows, list) or not rows:
        raise GateError("manifest test_files must be a nonempty list")
    seen: set[str] = set()
    for row in rows:
        required = {
            "path", "id", "domain", "primary_level", "fixture_owner",
            "runtime_dependencies", "isolation", "network", "destructive",
            "slow", "timeout_seconds", "max_output_bytes", "max_artifact_bytes",
        }
        if not isinstance(row, dict) or not required <= row.keys():
            raise GateError(f"manifest row lacks required fields: {row!r}")
        path = row["path"]
        if not isinstance(path, str) or Path(path).is_absolute() or ".." in Path(path).parts:
            raise GateError(f"invalid repository-relative test path: {path!r}")
        if path in seen:
            raise GateError(f"duplicate test path: {path}")
        seen.add(path)
        if row["primary_level"] not in LEVELS:
            raise GateError(f"{path}: invalid primary_level")
        if row["primary_level"] in {"L0", "L5", "L6"}:
            raise GateError(f"{path}: L0/L5/L6 are policies, not primary test levels")
        if not row["domain"] or not row["fixture_owner"] or not isinstance(row["runtime_dependencies"], list):
            raise GateError(f"{path}: domain, fixture_owner and runtime_dependencies are required")
        for field in ("timeout_seconds", "max_output_bytes", "max_artifact_bytes"):
            if not isinstance(row[field], int) or row[field] <= 0:
                raise GateError(f"{path}: {field} must be a positive integer")
        if row["slow"] not in (True, False, "unknown"):
            raise GateError(f"{path}: slow must be true, false or unknown")
        if not isinstance(row["destructive"], bool):
            raise GateError(f"{path}: destructive must be boolean")
        profile = row.get("execution_profile")
        if profile not in (None, "qualified-child-process", "hosted-isolated-compose"):
            raise GateError(f"{path}: unknown execution_profile")
        if row.get("native") is True and profile not in {"qualified-child-process", "hosted-isolated-compose"}:
            raise GateError(f"{path}: native suites require an explicit qualified execution profile")
    overrides = manifest.get("node_overrides", [])
    if not isinstance(overrides, list):
        raise GateError("node_overrides must be a list")
    override_ids: set[tuple[str, str]] = set()
    for row in overrides:
        key = (row.get("path", ""), row.get("node", ""))
        if key in override_ids or key[0] not in seen or not key[1]:
            raise GateError(f"invalid or duplicate node override: {row!r}")
        override_ids.add(key)
        if row.get("primary_level") not in LEVELS - {"L0", "L5", "L6"}:
            raise GateError(f"node override must have one primary level: {row!r}")
    rules = manifest.get("path_rules")
    if not isinstance(rules, list) or not rules:
        raise GateError("path_rules must be a nonempty deterministic mapping")
    for rule in rules:
        if not isinstance(rule.get("glob"), str) or not rule.get("domains"):
            raise GateError(f"invalid path rule: {rule!r}")
        for native_path in rule.get("required_native", []):
            native = next((row for row in rows if row["path"] == native_path), None)
            if native is None or native.get("native") is not True or native.get("opt_in") is not True:
                raise GateError(f"required_native must name a declared opt-in native suite: {native_path}")
    graph = manifest.get("domain_dependencies")
    if not isinstance(graph, dict):
        raise GateError("domain_dependencies must be a domain-to-list mapping")
    known_domains = {row["domain"] for row in rows} | {
        domain for rule in rules for domain in rule["domains"]
    }
    if set(graph) != known_domains:
        raise GateError("domain_dependencies keys must exactly match declared domains")
    for domain, dependencies in graph.items():
        if (not isinstance(dependencies, list)
                or any(not isinstance(item, str) or item not in known_domains for item in dependencies)
                or len(dependencies) != len(set(dependencies))):
            raise GateError(f"{domain}: dependencies must be unique declared domains")
    policies = manifest.get("policies", {})
    for policy in ("default", "L5", "L6", "L7"):
        if not isinstance(policies.get(policy), dict):
            raise GateError(f"missing {policy} policy")
    for level in policies["default"].get("required_levels", []):
        if level not in {"L0", "L1", "L2", "L3", "L4"}:
            raise GateError(f"invalid default required level: {level}")
    for item in policies["L5"].get("anchors", []):
        if item not in seen:
            raise GateError(f"unknown L5 anchor: {item}")


def discover_test_files() -> set[str]:
    files: set[str] = set()
    for pattern in TEST_FILE_GLOBS:
        files.update(p.relative_to(ROOT).as_posix() for p in ROOT.glob(pattern) if p.is_file())
    return files


def manifest_paths(manifest: dict[str, Any]) -> set[str]:
    return {row["path"] for row in manifest["test_files"]}


def check_catalog(manifest: dict[str, Any]) -> list[str]:
    expected = discover_test_files()
    catalog = manifest_paths(manifest)
    missing = sorted(expected - catalog)
    stale = sorted(path for path in catalog - expected if not (ROOT / path).is_file())
    errors = [f"unclassified test file: {p}" for p in missing]
    errors.extend(f"catalog path does not exist: {p}" for p in stale)
    return errors


def git(*args: str, required: bool = True) -> str:
    proc = subprocess.run(["git", *args], cwd=ROOT, text=True, capture_output=True, check=False)
    if required and proc.returncode:
        raise GateError(f"git {' '.join(args)} failed: {proc.stderr.strip()}")
    return proc.stdout.strip()


def changed_paths(base: str) -> list[str]:
    raw = subprocess.run(
        ["git", "diff", "--name-only", "-z", f"{base}...HEAD"],
        cwd=ROOT, capture_output=True, check=False,
    )
    if raw.returncode:
        raise GateError(f"cannot diff base {base!r}; fetch/resolve the immutable base first")
    return sorted({part.decode("utf-8", "strict") for part in raw.stdout.split(b"\0") if part})


def dependency_closure(domains: set[str], dependencies: dict[str, list[str]]) -> set[str]:
    """Return all transitively required domains, including the supplied roots.

    A visited set makes reviewed dependency cycles harmless and deterministic;
    output ordering is applied by callers when it is serialized.
    """
    expanded = set(domains)
    pending = list(sorted(domains, reverse=True))
    while pending:
        domain = pending.pop()
        for dependency in sorted(dependencies.get(domain, []), reverse=True):
            if dependency not in expanded:
                expanded.add(dependency)
                pending.append(dependency)
    return expanded


def required_execution_profiles(rows: list[dict[str, Any]]) -> list[str]:
    return sorted({row["execution_profile"] for row in rows if row.get("execution_profile")})


def diff_check(base_commit: str, head_commit: str = "HEAD", cwd: Path = ROOT) -> subprocess.CompletedProcess[bytes]:
    """Check whitespace in an explicit committed three-dot range."""
    return subprocess.run(
        ["git", "diff", "--check", f"{base_commit}...{head_commit}"],
        cwd=cwd, capture_output=True, timeout=20, check=False,
    )


def domain_for_path(path: str, manifest: dict[str, Any]) -> tuple[set[str], list[dict[str, Any]]]:
    matched = [rule for rule in manifest["path_rules"] if fnmatch.fnmatchcase(path, rule["glob"])]
    if not matched:
        raise GateError(f"changed path has no domain rule: {path}")
    generic = {"backend/fwrouter_api/**", "backend/fwrouter_api/adapters/**", "**/*.yml", "**/*.yaml", "**/*.md"}
    specific = [rule for rule in matched if rule["glob"] not in generic]
    if specific:
        matched = specific
    else:
        matched = [rule for rule in matched if rule["glob"] not in {"**/*.yml", "**/*.yaml", "**/*.md"}]
    if not matched:
        raise GateError(f"changed path has only an unclassified generic rule: {path}")
    domains = {domain for rule in matched for domain in rule["domains"]}
    return domains, matched


def make_plan(base: str, manifest: dict[str, Any], paths: list[str] | None = None, include_native: bool = False) -> dict[str, Any]:
    files = paths if paths is not None else changed_paths(base)
    if not files:
        raise GateError("empty change set cannot produce an eligibility plan")
    rows = {row["path"]: row for row in manifest["test_files"]}
    selected: set[str] = set()
    optional: set[str] = set()
    domains: set[str] = set()
    required_native: set[str] = set()
    matches: list[dict[str, Any]] = []
    for path in files:
        resolved_domains, matched = domain_for_path(path, manifest)
        domains.update(resolved_domains)
        for rule in matched:
            required_native.update(rule.get("required_native", []))
        if path in rows:
            domains.add(rows[path]["domain"])
            (selected if include_native or not rows[path].get("opt_in") else optional).add(path)
        matches.extend({"path": path, "rule": rule["glob"], "domains": sorted(rule["domains"])} for rule in matched)
    for path, row in rows.items():
        if row["domain"] in domains:
            (selected if include_native or not row.get("opt_in") else optional).add(path)
    dependencies = manifest.get("domain_dependencies", {})
    expanded = dependency_closure(domains, dependencies)
    for path, row in rows.items():
        if row["domain"] in expanded:
            (selected if include_native or not row.get("opt_in") else optional).add(path)
    for path in required_native:
        if path not in rows:
            raise GateError(f"path rule requires an unknown native suite: {path}")
        selected.add(path)
        optional.discard(path)
    required_levels = set(manifest["policies"]["default"]["required_levels"])
    required_levels.add("L0")
    for path in selected:
        required_levels.add(rows[path]["primary_level"])
    # L5 is an explicit aggregation reason over stable anchors; it does not
    # promote or duplicate those tests into a second primary level.
    shared_reasons = sorted(set(domains) & set(manifest["policies"]["L5"].get("shared_domains", [])))
    l5_anchors = sorted(manifest["policies"]["L5"]["anchors"]) if shared_reasons else []
    selected.update(l5_anchors)
    optional.difference_update(selected)
    required_levels.update(rows[path]["primary_level"] for path in l5_anchors)
    plan = {
        "schema_version": 1,
        "created_utc": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "source_commit": git("rev-parse", "HEAD"),
        "base_commit": git("rev-parse", f"{base}^{{commit}}"),
        "manifest_version": manifest["version"],
        "manifest_digest": canonical_digest(manifest),
        "changed_paths": sorted(files),
        "path_matches": sorted(matches, key=lambda item: (item["path"], item["rule"])),
        "domains": sorted(domains),
        "dependency_domains": sorted(expanded - domains),
        "selected_files": sorted(selected),
        "optional_suites": sorted(optional),
        "required_native_suites": sorted(required_native),
        "required_execution_profiles": required_execution_profiles([rows[path] for path in selected]),
        "include_native": include_native,
        "required_levels": sorted(required_levels),
        "regression_policy": {"level": "L5", "reasons": shared_reasons, "anchors": l5_anchors},
        "full_suite_required": False,
        "staging_required": False,
    }
    plan["plan_digest"] = canonical_digest(plan)
    return plan


def make_quick_push_plan(base: str, manifest: dict[str, Any], paths: list[str] | None = None) -> dict[str, Any]:
    """Build the quick L1 lane, deferring profile-bound suites to affected CI."""
    plan = make_plan(base, manifest, paths, include_native=False)
    rows = {row["path"]: row for row in manifest["test_files"]}
    plan["quick_deferred_profile_suites"] = sorted(
        path for path in plan["selected_files"]
        if rows[path]["primary_level"] == "L1" and rows[path].get("execution_profile")
    )
    plan["selected_files"] = sorted(
        path for path in plan["selected_files"]
        if rows[path]["primary_level"] == "L1" and not rows[path].get("execution_profile")
    )
    plan["optional_suites"] = sorted(
        path for path in plan["optional_suites"] if rows[path]["primary_level"] == "L1"
    )
    plan["required_native_suites"] = []
    plan["required_execution_profiles"] = required_execution_profiles(
        [rows[path] for path in plan["selected_files"]]
    )
    plan["required_levels"] = ["L0", "L1"] if plan["selected_files"] else ["L0"]
    plan["regression_policy"] = {"level": "L5", "reasons": [], "anchors": []}
    plan["quick_push"] = True
    plan["plan_digest"] = canonical_digest({key: value for key, value in plan.items() if key != "plan_digest"})
    return plan


def atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    if len(payload.encode("utf-8")) > MAX_REPORT_BYTES:
        raise GateError(f"evidence artifact exceeds {MAX_REPORT_BYTES} byte cap")
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def print_json(value: Any) -> None:
    print(json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2))


def ensure_plan(plan: dict[str, Any], manifest: dict[str, Any], allow_l6: bool = False,
                allow_manual_subset: bool = False) -> None:
    provided = dict(plan)
    digest = provided.pop("plan_digest", None)
    if not digest or digest != canonical_digest(provided):
        raise GateError("change plan digest is invalid")
    if not plan.get("changed_paths"):
        raise GateError("empty change plan cannot grant test or deploy eligibility")
    if plan.get("manifest_digest") != canonical_digest(manifest):
        raise GateError("plan was generated from a different manifest revision")
    if plan.get("manifest_version") != manifest["version"]:
        raise GateError("plan manifest version mismatch")
    if plan.get("source_commit") != git("rev-parse", "HEAD"):
        raise GateError("source commit changed after plan generation")
    allowed = {"L0", "L1", "L2", "L3", "L4", "L6"} if allow_l6 else {"L0", "L1", "L2", "L3", "L4"}
    if not set(plan.get("required_levels", [])) <= allowed:
        raise GateError("plan requests a forbidden level")
    if plan.get("full_suite_required"):
        l6_paths = {row["path"] for row in manifest["test_files"] if row["primary_level"] != "L7"}
        if not allow_l6 or plan.get("manual_gate") != "L6" or set(plan.get("selected_files", [])) != l6_paths:
            raise GateError("L6 requires a verified manual/nightly/release full-suite plan")
        expected_levels = {"L0", *(row["primary_level"] for row in manifest["test_files"] if row["primary_level"] != "L7")}
        if set(plan.get("required_levels", [])) != expected_levels or plan.get("changed_paths") != ["manual-policy:L6"]:
            raise GateError("L6 plan does not request exactly the declared full-suite levels")
    elif plan.get("quick_push"):
        if allow_l6 or allow_manual_subset or plan.get("include_native"):
            raise GateError("quick push plans cannot be combined with manual/native suite modes")
        actual_paths = changed_paths(plan["base_commit"])
        if sorted(actual_paths) != sorted(plan["changed_paths"]):
            raise GateError("quick push plan paths do not match the actual immutable base..HEAD diff")
        expected = make_quick_push_plan(plan["base_commit"], manifest, actual_paths)
        fields = ("source_commit", "base_commit", "changed_paths", "path_matches", "domains",
                  "dependency_domains", "selected_files", "optional_suites", "required_native_suites",
                  "required_execution_profiles", "required_levels", "regression_policy", "quick_push",
                  "quick_deferred_profile_suites")
        if any(expected.get(field) != plan.get(field) for field in fields):
            raise GateError("quick push plan is not the deterministic affected L1 selection")
    elif plan.get("manual_subset"):
        if not allow_manual_subset or plan.get("manual_gate") != "subset":
            raise GateError("manual subset requires explicit --manual-subset and is not a promotion plan")
        selector = plan.get("subset_selector", {})
        level = selector.get("level")
        domain = selector.get("domain")
        if level not in {None, "L1", "L2", "L3", "L4"} or (not level and not domain):
            raise GateError("manual subset selector must name a domain or one primary level L1-L4")
        if domain is not None and domain not in {row["domain"] for row in manifest["test_files"]}:
            raise GateError("manual subset domain is not present in the manifest")
        selected_rows = [row for row in manifest["test_files"]
                         if (domain is None or row["domain"] == domain)
                         and (level is None or row["primary_level"] == level)
                         and (plan.get("include_native") or not row.get("opt_in"))]
        if set(plan["selected_files"]) != {row["path"] for row in selected_rows}:
            raise GateError("manual subset selection does not match its manifest selector")
        if set(plan["required_levels"]) != {"L0", *(row["primary_level"] for row in selected_rows)}:
            raise GateError("manual subset levels do not match selected primary levels")
        if plan.get("changed_paths") != [f"manual-subset:{domain or '*'}:{level or '*'}"]:
            raise GateError("manual subset selector evidence is invalid")
        if any(row["primary_level"] == "L7" or row.get("destructive") for row in selected_rows):
            raise GateError("L7/destructive tests cannot be selected by the manual subset runner")
    elif allow_l6:
        raise GateError("--manual-full-suite requires an L6 plan")
    if plan.get("staging_required"):
        raise GateError("staging plans require a separate disposable-stage runner")
    if not set(plan["selected_files"]) <= manifest_paths(manifest):
        raise GateError("plan references a test file absent from the current manifest")
    if not plan.get("full_suite_required") and not plan.get("manual_subset") and not plan.get("quick_push"):
        actual_paths = changed_paths(plan["base_commit"])
        if sorted(actual_paths) != sorted(plan["changed_paths"]):
            raise GateError("change plan paths do not match the actual immutable base..HEAD diff")
        expected = make_plan(plan["base_commit"], manifest, actual_paths, include_native=plan.get("include_native", False))
        for field in ("path_matches", "domains", "dependency_domains", "selected_files", "optional_suites", "required_native_suites", "required_execution_profiles", "include_native", "required_levels", "regression_policy"):
            if expected[field] != plan.get(field):
                raise GateError(f"change plan {field} is not the deterministic manifest result")
    expected_profiles = required_execution_profiles(
        [row for row in manifest["test_files"] if row["path"] in set(plan["selected_files"])]
    )
    if plan.get("required_execution_profiles") != expected_profiles:
        raise GateError("plan execution profiles do not match selected manifest suites")


def run_process(argv: list[str], timeout: int, output_limit: int, cwd: Path = ROOT, env: dict[str, str] | None = None) -> tuple[int, str, bool]:
    chunks: queue.Queue[bytes | None] = queue.Queue(maxsize=8)
    creationflags = subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
    proc = subprocess.Popen(
        argv, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        start_new_session=(os.name != "nt"), creationflags=creationflags, env=env,
    )
    assert proc.stdout is not None

    def reader() -> None:
        try:
            while True:
                block = proc.stdout.read(8192)
                if not block:
                    break
                chunks.put(block)
        finally:
            chunks.put(None)

    thread = threading.Thread(target=reader, name="fwrouter-gate-output", daemon=True)
    thread.start()
    output = bytearray()
    output_tail = bytearray()
    seen = 0
    exceeded = False
    timed_out = False
    deadline = time.monotonic() + timeout
    finished = False
    termination_sent = False
    force_attempts = 0
    while not finished:
        remaining = deadline - time.monotonic()
        if remaining <= 0 and not termination_sent:
            timed_out = True
            _terminate_process_group(proc)
            termination_sent = True
            deadline = time.monotonic() + 5
        try:
            block = chunks.get(timeout=0.05 if remaining > 0 else 0.2)
        except queue.Empty:
            if proc.poll() is not None and not thread.is_alive():
                finished = True
            elif termination_sent and (timed_out or exceeded) and time.monotonic() > deadline:
                _terminate_process_group(proc, force=True)
                force_attempts += 1
                if force_attempts >= 2:
                    if proc.stdout:
                        proc.stdout.close()
                    finished = True
                deadline = time.monotonic() + 2
            continue
        if block is None:
            finished = proc.poll() is not None
            continue
        seen += len(block)
        tail_limit = max(1024, output_limit // 2)
        output_tail.extend(block)
        if len(output_tail) > tail_limit:
            del output_tail[:-tail_limit]
        if len(output) < output_limit:
            output.extend(block[: output_limit - len(output)])
        if seen > output_limit and not exceeded:
            exceeded = True
            _terminate_process_group(proc)
            termination_sent = True
            timed_out = False
            deadline = time.monotonic() + 5
    try:
        returncode = proc.wait(timeout=1)
    except subprocess.TimeoutExpired:
        _terminate_process_group(proc, force=True)
        returncode = proc.wait(timeout=1)
    thread.join(timeout=1)
    try:
        proc.stdout.close()
    except OSError:
        pass
    code = 124 if timed_out else (125 if exceeded else returncode)
    if exceeded:
        prefix_limit = max(1024, output_limit // 2)
        retained = output[:prefix_limit] + b"\n...[bounded output omitted]...\n" + output_tail
    else:
        retained = output
    return code, retained.decode("utf-8", "replace"), exceeded


def redact_failure_output(value: str) -> str:
    """Keep useful bounded failure context without persisting common secrets."""
    value = re.sub(r"(?i)(bearer\s+)[A-Za-z0-9._~+/-]+=*", r"\1[REDACTED]", value)
    value = re.sub(
        r"(?i)((?:authorization|password|passwd|token|secret|api[_-]?key|client[_-]?secret)\s*[:=]\s*[\"']?)[^\s,;\"']+",
        r"\1[REDACTED]", value,
    )
    value = re.sub(r"(://[^:/\s@]+:)[^@/\s]+@", r"\1[REDACTED]@", value)
    return value[-8192:]


def _terminate_process_group(proc: subprocess.Popen[bytes], force: bool = False) -> None:
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"], capture_output=True, timeout=3, check=False)
        else:
            os.killpg(proc.pid, signal.SIGKILL if force else signal.SIGTERM)
    except (OSError, ProcessLookupError):
        pass


def load_baseline(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise GateError(f"cannot read baseline policy: {exc}") from exc
    if data.get("schema_version") != 1:
        raise GateError("baseline evidence requires schema_version 1")
    baseline_commit = data.get("baseline_commit") or data.get("historical_baseline_commit") or data.get("source_baseline_commit")
    if not baseline_commit:
        raise GateError("baseline evidence requires an explicit baseline commit")
    if "failures" not in data:
        nodes = data.get("nodes", [])
        ui = data.get("ui_historical_baseline")
        if isinstance(ui, dict) and ui.get("node_suite_id"):
            # Preserve the historical Node TAP suite ID as evidence, while
            # map it to its repository-relative manifest path for selection.
            nodes = [*nodes, {**ui, "nodeid": "ui/" + ui["node_suite_id"]}]
        failures = []
        for node in nodes:
            if not isinstance(node, dict) or not node.get("nodeid"):
                raise GateError("baseline nodes require exact nodeid")
            status = "approved_exception" if data.get("approved_baseline_exception") is True and node.get("approved") is True else "unverified"
            failures.append({
                "nodeid": node["nodeid"], "status": status,
                "owner": node.get("owner"), "remediation": node.get("remediation"),
                "evidence": node.get("evidence"), "expires_on": node.get("expires_on"),
            })
        data = {**data, "baseline_commit": baseline_commit, "failures": failures}
    if not isinstance(data.get("failures"), list):
        raise GateError("baseline failures must be an exact list")
    ids: set[str] = set()
    for row in data["failures"]:
        nodeid = row.get("nodeid")
        if not nodeid or nodeid in ids:
            raise GateError("baseline node IDs must be unique and exact")
        ids.add(nodeid)
        if row.get("status") not in {"unverified", "fixed", "approved_exception", "retired"}:
            raise GateError(f"baseline status is invalid for {nodeid}")
        if row["status"] == "approved_exception":
            expiry = row.get("expires_on")
            if not row.get("owner") or not row.get("remediation") or not row.get("evidence") or not expiry:
                raise GateError(f"baseline exception lacks owner/remediation/evidence/expiry: {nodeid}")
            if date.fromisoformat(expiry) < date.today():
                raise GateError(f"baseline exception expired: {nodeid}")
    return data


def classify(plan: dict[str, Any], execution: dict[str, Any], baseline: dict[str, Any]) -> dict[str, Any]:
    statuses = execution.get("node_status", {})
    if not isinstance(statuses, dict):
        raise GateError("execution node_status must be a mapping of exact IDs to statuses")
    invalid = sorted({status for status in statuses.values() if status not in {"passed", "failed", "skipped"}})
    if invalid:
        raise GateError(f"execution contains unknown node statuses: {invalid}")
    baseline_rows = {canonical_node_id(row["nodeid"]): row for row in baseline["failures"]}
    selected_paths = set(plan["selected_files"])
    current_paths = {node.split("::", 1)[0] for node in statuses}
    baseline_selected = {
        node: row for node, row in baseline_rows.items()
        if canonical_test_path(node.split("::", 1)[0]) in selected_paths
    }
    novel: list[str] = []
    existing: list[str] = []
    fixed: list[str] = []
    unresolved: list[str] = []
    pending: list[str] = []
    retired: list[str] = []
    for node, status in statuses.items():
        if status == "failed":
            record = baseline_rows.get(node)
            if record is None:
                novel.append(node)
            elif record["status"] == "approved_exception":
                existing.append(node)
            else:
                unresolved.append(node)
        elif status == "passed" and node in baseline_rows:
            fixed.append(node)
        elif status == "skipped" and node in baseline_rows:
            pending.append(node)
    retire_map = baseline.get("retirements", {})
    for node in baseline_selected:
        if node not in statuses:
            record = retire_map.get(node)
            if isinstance(record, dict) and record.get("reviewed") is True and record.get("evidence"):
                retired.append(node)
            else:
                pending.append(node)
    # A baseline failure outside this plan is pending, never fixed/disappeared.
    unselected = sorted(set(baseline_rows) - set(baseline_selected))
    result = {
        "schema_version": 1,
        "source_commit": plan["source_commit"],
        "baseline_commit": baseline["baseline_commit"],
        "plan_digest": plan["plan_digest"],
        "selected_files": sorted(selected_paths),
        "executed_node_count": len(statuses),
        "novel_failures": sorted(novel),
        "baseline_failures_unapproved": sorted(unresolved),
        "approved_baseline_failures": sorted(existing),
        "baseline_failures_passed_in_this_run": sorted(fixed),
        "baseline_failures_skipped_or_uncollected": sorted(set(pending)),
        "baseline_failures_retired_with_evidence": sorted(retired),
        "baseline_failures_unselected_pending": unselected,
        "eligible": not novel and not unresolved and not pending,
    }
    return result


def promote(plan: dict[str, Any], manifest: dict[str, Any], reports: list[dict[str, Any]]) -> dict[str, Any]:
    if plan.get("manual_subset"):
        raise GateError("manual subset evidence cannot establish deploy eligibility")
    if plan.get("quick_push"):
        raise GateError("fast push evidence cannot establish reviewed deploy eligibility")
    ensure_plan(plan, manifest)
    if git("status", "--porcelain", "--untracked-files=all"):
        raise GateError("promotion eligibility requires a clean immutable source checkout")
    required = set(plan["required_levels"])
    covered: set[str] = set()
    for report in reports:
        recorded_digest = report.get("report_digest")
        unsigned = dict(report)
        unsigned.pop("report_digest", None)
        if not recorded_digest or recorded_digest != canonical_digest(unsigned):
            raise GateError("evidence report digest is missing or invalid")
        if report.get("source_commit") != plan["source_commit"] or report.get("plan_digest") != plan["plan_digest"]:
            raise GateError("evidence report does not match immutable source commit and plan")
        if report.get("manifest_version") != manifest["version"]:
            raise GateError("evidence report manifest version does not match")
        if report.get("manifest_digest") != canonical_digest(manifest):
            raise GateError("evidence report manifest digest does not match")
        if report.get("tool_version") != TOOL_VERSION:
            raise GateError("evidence report test-gate tool version does not match")
        if report.get("eligible") is not True:
            raise GateError("one or more required evidence reports are blocked")
        levels = set(report.get("completed_levels", []))
        if not levels or "L0" not in levels or report.get("l0", {}).get("status") != "passed":
            raise GateError("evidence report lacks nonempty L0 and test-level evidence")
        if not report.get("executions"):
            raise GateError("evidence report has no executed suites")
        if any(row.get("status") != "passed" for row in report["executions"]):
            raise GateError("evidence report contains a non-passing suite")
        if report.get("classification", {}).get("eligible") is not True:
            raise GateError("evidence report lacks passing exact-ID baseline classification")
        classification = report["classification"]
        if (classification.get("source_commit") != plan["source_commit"]
                or classification.get("plan_digest") != plan["plan_digest"]
                or set(classification.get("selected_files", [])) != set(plan["selected_files"])):
            raise GateError("baseline classification does not match the immutable plan")
        if report.get("l0", {}).get("status") != "passed":
            raise GateError("evidence report lacks a passing L0 record")
        expected_files = set(plan["selected_files"])
        actual_files = {row.get("path") for row in report.get("executions", []) if row.get("path") != "@L0"}
        if actual_files != expected_files:
            missing = sorted(expected_files - actual_files)
            extra = sorted(actual_files - expected_files)
            raise GateError(f"evidence suite coverage mismatch; missing={missing}, extra={extra}")
        expected_rows = {row["path"]: row for row in manifest["test_files"]}
        suite_rows = [row for row in report["executions"] if row.get("path") != "@L0"]
        if len(suite_rows) != len(expected_files):
            raise GateError("evidence report contains duplicate suite executions")
        for row in suite_rows:
            expected_row = expected_rows[row["path"]]
            if row.get("suite") != expected_row["id"] or row.get("primary_level") != expected_row["primary_level"]:
                raise GateError("evidence suite ID or primary level disagrees with manifest")
        valid_statuses = {"passed"}
        if any(row.get("status") not in valid_statuses for row in report["executions"]):
            raise GateError("evidence report contains an unknown, skipped or blocked status")
        expected_levels = {"L0", *(next(row["primary_level"] for row in manifest["test_files"] if row["path"] == path) for path in expected_files)}
        execution_levels = {row.get("primary_level") for row in report["executions"] if row.get("path") != "@L0"}
        if levels != expected_levels or not expected_levels <= execution_levels | {"L0"}:
            raise GateError("evidence completed levels do not match selected suite primary levels")
        covered.update(levels)
    missing = sorted(required - covered)
    return {
        "schema_version": 1,
        "source_commit": plan["source_commit"],
        "plan_digest": plan["plan_digest"],
        "required_levels": sorted(required),
        "completed_levels": sorted(covered),
        "missing_levels": missing,
        "eligible_for_reviewed_deploy": bool(required) and not missing,
        "deploy_performed": False,
    }


def command_validate(args: argparse.Namespace) -> int:
    manifest = load_manifest(Path(args.manifest))
    errors = check_catalog(manifest)
    if errors:
        for error in errors:
            print(f"FAIL: {error}", file=sys.stderr)
        return 2
    print(f"PASS: manifest {manifest['version']} covers {len(manifest['test_files'])} test files")
    return 0


def command_plan(args: argparse.Namespace) -> int:
    manifest = load_manifest(Path(args.manifest))
    catalog_errors = check_catalog(manifest)
    if catalog_errors:
        raise GateError("manifest/catalog mismatch: " + "; ".join(catalog_errors))
    if getattr(args, "quick_push", False):
        if args.include_native:
            raise GateError("quick push plan cannot include native opt-in suites")
        plan = make_quick_push_plan(args.base, manifest, args.paths or None)
    else:
        plan = make_plan(args.base, manifest, args.paths or None, include_native=args.include_native)
    if args.output:
        atomic_json(Path(args.output), plan)
    print_json(plan)
    return 0


def command_subset_plan(args: argparse.Namespace) -> int:
    manifest = load_manifest(Path(args.manifest))
    errors = check_catalog(manifest)
    if errors:
        raise GateError("manifest/catalog mismatch: " + "; ".join(errors))
    if not args.domain and not args.level:
        raise GateError("specify --domain and/or --level for a manual subset")
    domains = {row["domain"] for row in manifest["test_files"]}
    if args.domain and args.domain not in domains:
        raise GateError("unknown domain")
    selected_rows = [row for row in manifest["test_files"]
                     if (not args.domain or row["domain"] == args.domain)
                     and (not args.level or row["primary_level"] == args.level)
                     and (args.include_native or not row.get("opt_in"))]
    if not selected_rows:
        raise GateError("manual subset selected no test files")
    selected_files = sorted(row["path"] for row in selected_rows)
    plan = {
        "schema_version": 1,
        "created_utc": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "source_commit": git("rev-parse", "HEAD"),
        "base_commit": git("rev-parse", "HEAD"),
        "manifest_version": manifest["version"],
        "manifest_digest": canonical_digest(manifest),
        "changed_paths": [f"manual-subset:{args.domain or '*'}:{args.level or '*'}"],
        "domains": [args.domain] if args.domain else [],
        "selected_files": selected_files,
        "optional_suites": [],
        "required_native_suites": [row["path"] for row in selected_rows if row.get("native")],
        "required_execution_profiles": required_execution_profiles(selected_rows),
        "include_native": args.include_native,
        "required_levels": sorted({"L0", *(row["primary_level"] for row in selected_rows)}),
        "regression_policy": {"level": "manual-subset", "anchors": []},
        "full_suite_required": False,
        "manual_subset": True,
        "manual_gate": "subset",
        "subset_selector": {"domain": args.domain, "level": args.level},
        "deploy_eligibility": False,
    }
    plan["plan_digest"] = canonical_digest(plan)
    if args.output:
        atomic_json(Path(args.output), plan)
    print_json(plan)
    return 0


def command_dry_run(args: argparse.Namespace) -> int:
    manifest = load_manifest(Path(args.manifest))
    if args.level == "L7":
        result = {
            "requested_level": "L7",
            "mode": "dry-run",
            "selected_tests": [],
            "actual_staging_execution": False,
            "result": "BLOCKED_NO_DISPOSABLE_STAGING_ATTESTATION",
        }
        print_json(result)
        return 0
    if args.level == "L6" and not args.manual:
        raise GateError("L6 requires explicit --manual; it is not part of default/affected gates")
    if args.level == "L6":
        selected = [row["path"] for row in manifest["test_files"]]
    elif args.level == "L5":
        selected = list(manifest["policies"]["L5"]["anchors"])
    else:
        selected = [row["path"] for row in manifest["test_files"] if row["primary_level"] == args.level]
    print_json({"requested_level": args.level, "selected_files": sorted(selected), "executed": False})
    return 0


def command_subset_plan_entry(args: argparse.Namespace) -> int:
    return command_subset_plan(args)


def command_classify(args: argparse.Namespace) -> int:
    manifest = load_manifest(Path(args.manifest))
    plan = json.loads(Path(args.plan).read_text(encoding="utf-8"))
    ensure_plan(plan, manifest, allow_manual_subset=bool(plan.get("manual_subset")))
    execution = json.loads(Path(args.execution).read_text(encoding="utf-8"))
    baseline = load_baseline(Path(args.baseline))
    result = classify(plan, execution, baseline)
    if args.output:
        atomic_json(Path(args.output), result)
    print_json(result)
    return 0 if result["eligible"] else 1


def command_promote(args: argparse.Namespace) -> int:
    manifest = load_manifest(Path(args.manifest))
    plan = json.loads(Path(args.plan).read_text(encoding="utf-8"))
    reports = [json.loads(Path(p).read_text(encoding="utf-8")) for p in args.reports]
    result = promote(plan, manifest, reports)
    if args.output:
        atomic_json(Path(args.output), result)
    print_json(result)
    return 0 if result["eligible_for_reviewed_deploy"] else 1


def command_smoke(args: argparse.Namespace) -> int:
    import importlib.util
    smoke_path = GATES / "smoke.py"
    spec = importlib.util.spec_from_file_location("fwrouter_gate_smoke", smoke_path)
    if spec is None or spec.loader is None:
        raise GateError("isolated smoke helper is unavailable")
    smoke_module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(smoke_module)
    run_smoke = smoke_module.run_smoke
    validators = None
    if args.validators_json:
        validators = json.loads(Path(args.validators_json).read_text(encoding="utf-8"))
        if not isinstance(validators, list):
            raise GateError("--validators-json must contain a JSON list")
    failed_units = None
    if args.failed_units_allowlist_json:
        failed_units = json.loads(Path(args.failed_units_allowlist_json).read_text(encoding="utf-8"))
        if not isinstance(failed_units, list):
            raise GateError("--failed-units-allowlist-json must contain a JSON list")
    report = run_smoke(
        args.profile, reason=args.reason, opt_in=args.opt_in,
        db_path=args.db_path, api_base_url=args.api_base_url,
        validators=validators, failed_units_allowlist=failed_units,
        require_native=(args.complete_native or args.profile == "live-readonly"),
    )
    print_json(report)
    return 0 if report.get("status") == "passed" else 1


def command_full_suite_plan(args: argparse.Namespace) -> int:
    if not args.manual:
        raise GateError("L6 requires explicit --manual")
    manifest = load_manifest(Path(args.manifest))
    errors = check_catalog(manifest)
    if errors:
        raise GateError("manifest/catalog mismatch: " + "; ".join(errors))
    selected = sorted(row["path"] for row in manifest["test_files"] if row["primary_level"] != "L7")
    levels = sorted({"L0", *(row["primary_level"] for row in manifest["test_files"] if row["primary_level"] != "L7")})
    plan = {
        "schema_version": 1,
        "created_utc": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "source_commit": git("rev-parse", "HEAD"),
        "base_commit": git("rev-parse", "HEAD"),
        "manifest_version": manifest["version"],
        "manifest_digest": canonical_digest(manifest),
        "changed_paths": ["manual-policy:L6"],
        "path_matches": [],
        "domains": sorted({row["domain"] for row in manifest["test_files"]}),
        "dependency_domains": [],
        "selected_files": selected,
        "optional_suites": [],
        "required_native_suites": [],
        "required_execution_profiles": required_execution_profiles(
            [row for row in manifest["test_files"] if row["path"] in set(selected)]
        ),
        "include_native": args.include_native,
        "required_levels": levels,
        "regression_policy": {"level": "L6", "reasons": ["manual/nightly/release"], "anchors": []},
        "full_suite_required": True,
        "manual_gate": "L6",
        "staging_required": False,
    }
    plan["plan_digest"] = canonical_digest(plan)
    if args.output:
        atomic_json(Path(args.output), plan)
    print_json(plan)
    return 0


def canonical_test_path(path: str) -> str:
    normalized = path.replace("\\", "/")
    if (normalized.startswith("tests/") and not normalized.startswith("tests/gates/")
            and not normalized.startswith("tests/application_acceptance/")):
        normalized = "backend/" + normalized
    if normalized.startswith("ui/tests/") or normalized.startswith("installer/") or normalized.startswith("integrations/"):
        return normalized
    return normalized


def canonical_node_id(nodeid: str) -> str:
    path, sep, tail = nodeid.partition("::")
    return canonical_test_path(path) + (sep + tail if sep else "")


def clean_test_environment(owned: Path) -> dict[str, str]:
    (owned / ".fwrouter-test-run-owned").write_text("gate-runner\n", encoding="utf-8")
    (owned / "home").mkdir(exist_ok=True)
    (owned / "tmp").mkdir(exist_ok=True)
    source_path = os.environ.get("PATH", os.defpath)
    env = {
        "PATH": source_path, "HOME": str(owned / "home"), "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8", "TZ": "UTC",
        "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1", "PYTHONDONTWRITEBYTECODE": "1",
    }
    if os.name == "nt":
        env.update({"SYSTEMROOT": os.environ.get("SYSTEMROOT", ""), "TEMP": str(owned / "tmp"), "TMP": str(owned / "tmp"), "USERPROFILE": str(owned / "home")})
    else:
        env.update({"TMPDIR": str(owned / "tmp")})
    state = owned / "state"
    for name, value in {
        "FWROUTER_ENVIRONMENT": "test", "FWROUTER_STATE_DIR": str(state),
        "FWROUTER_STARTUP_RECOVERY_ENABLED": "false", "FWROUTER_WATCHDOG_SCHEDULER_ENABLED": "false",
        "FWROUTER_MAINTENANCE_SCHEDULER_ENABLED": "false", "FWROUTER_MEMBER_PROBE_SCHEDULER_ENABLED": "false",
        "FWROUTER_ACTIVE_OBSERVATION_SCHEDULER_ENABLED": "false", "FWROUTER_SUBJECT_INVENTORY_SCHEDULER_ENABLED": "false",
        "FWROUTER_EXTERNAL_COLLECTOR_SCHEDULER_ENABLED": "false", "FWROUTER_RUNTIME_CONVERGENCE_SCHEDULER_ENABLED": "false",
        # The coordinator owns and removes this shared root only after all
        # sequential suites finish. Do not let an individual pytest child
        # invoke conftest's one-suite cleanup on the shared parent.
        "FWROUTER_TEST_PROFILE": "routine",
    }.items():
        env[name] = value
    return env


def create_suite_root(owned: Path, suite_id: str) -> tuple[Path, Path]:
    """Create a private, parent-cleaned coordinator root for one pytest suite."""
    safe_id = hashlib.sha256(suite_id.encode("utf-8")).hexdigest()[:16]
    root = owned / "suites" / safe_id
    root.mkdir(parents=True, mode=0o700)
    (root / ".fwrouter-gate-test-root-owned").write_text(
        "FWROUTER_GATE_TEST_ROOT_V1\n", encoding="utf-8"
    )
    reports = root / "reports"
    reports.mkdir(mode=0o700)
    return root, reports


def environment_evidence(env: dict[str, str]) -> dict[str, str]:
    """Record tool versions without inheriting credentials or retaining output."""
    try:
        import importlib.metadata
        pytest_version = importlib.metadata.version("pytest")
    except importlib.metadata.PackageNotFoundError:
        pytest_version = "unavailable"
    node = shutil.which("node", path=env.get("PATH", os.defpath))
    node_version = "unavailable"
    if node:
        try:
            probe = subprocess.run([node, "--version"], capture_output=True, text=True, timeout=3, check=False, env=env)
            if probe.returncode == 0 and re.fullmatch(r"v\d+\.\d+\.\d+\s*", probe.stdout):
                node_version = probe.stdout.strip()
        except (OSError, subprocess.TimeoutExpired):
            pass
    return {"python": sys.version.split()[0], "pytest": pytest_version, "node": node_version}


def node_status_from_plugin(path: Path, max_bytes: int) -> dict[str, str]:
    if not path.is_file():
        return {}
    if path.stat().st_size > max_bytes:
        raise GateError("pytest exact-node report exceeded its artifact bound")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise GateError("pytest exact-node report is invalid") from exc
    statuses = payload.get("node_status") if payload.get("schema_version") == 1 else None
    if not isinstance(statuses, dict) or any(value not in {"passed", "failed", "skipped"} for value in statuses.values()):
        raise GateError("pytest exact-node report has unknown schema or status")
    return statuses


def node_status_from_tap(output: str, test_path: str) -> dict[str, str]:
    # Node's TAP reporter gives exact nested test names. Preserve them as raw
    # names with their file path so historical file-level IDs remain distinct.
    statuses: dict[str, str] = {}
    stack: list[tuple[int, str]] = []
    for line in output.splitlines():
        indent = len(line) - len(line.lstrip(" "))
        text = line.strip()
        subtest = re.match(r"#\s+Subtest:\s+(.+)$", text)
        if subtest:
            while stack and stack[-1][0] >= indent:
                stack.pop()
            stack.append((indent, subtest.group(1)))
            continue
        match = re.match(r"(?:ok|not ok)\s+\d+\s+-\s+(.+?)(?:\s+#\s+(SKIP|TODO).*)?$", text)
        if not match:
            continue
        while stack and stack[-1][0] >= indent:
            stack.pop()
        name, directive = match.groups()
        parts = [entry[1] for entry in stack] + [name]
        node = test_path + "::" + " > ".join(parts)
        status = "failed" if text.startswith("not ok") else ("skipped" if directive == "SKIP" else "passed")
        statuses[node] = status
        stack.append((indent, name))
    if not statuses and "not ok" in output:
        statuses[f"{test_path}::unparsed-tap-failure"] = "failed"
    elif not statuses:
        statuses[f"{test_path}::node-test-file"] = "passed"
    statuses[test_path] = "failed" if any(value == "failed" for value in statuses.values()) else (
        "skipped" if statuses and all(value == "skipped" for value in statuses.values()) else "passed"
    )
    return statuses


def run_l0(changed: list[str], owned: Path, base_commit: str) -> tuple[bool, list[str]]:
    problems: list[str] = []
    diff = diff_check(base_commit, cwd=ROOT)
    if diff.returncode:
        problems.append("git diff --check failed")
    for relative in changed:
        path = ROOT / relative
        if not path.is_file():
            continue
        try:
            if relative.endswith(".py"):
                import ast
                ast.parse(path.read_text(encoding="utf-8"), filename=relative)
            elif relative.endswith(".sh"):
                code, _, truncated = run_process(["sh", "-n", relative], 15, 32768, env=clean_test_environment(owned))
                if code or truncated:
                    problems.append(f"shell syntax failed: {relative}")
            elif relative.endswith(".js"):
                node = shutil.which("node", path=os.environ.get("PATH", os.defpath))
                if node is None:
                    problems.append(f"Node.js is required for L0 syntax check: {relative}")
                else:
                    code, _, truncated = run_process([node, "--check", relative], 15, 32768, env=clean_test_environment(owned))
                    if code or truncated:
                        problems.append(f"JavaScript syntax failed: {relative}")
            elif relative.endswith(".json"):
                json.loads(path.read_text(encoding="utf-8"))
            elif relative.endswith((".yml", ".yaml")):
                try:
                    import yaml
                except ImportError:
                    problems.append(f"PyYAML is required for changed YAML L0 validation: {relative}")
                else:
                    yaml.safe_load(path.read_text(encoding="utf-8"))
        except Exception as exc:
            problems.append(f"L0 parse failed for {relative}: {type(exc).__name__}")
    return not problems, problems


def command_run(args: argparse.Namespace) -> int:
    manifest = load_manifest(Path(args.manifest))
    plan = json.loads(Path(args.plan).read_text(encoding="utf-8"))
    ensure_plan(plan, manifest, allow_l6=args.manual_full_suite,
                allow_manual_subset=args.manual_subset)
    if args.include_native != bool(plan.get("include_native")):
        raise GateError("--include-native must match the immutable plan selection")
    owned = Path(tempfile.mkdtemp(
        prefix=f"fwrouter-gate-{plan['source_commit'][:8]}-", dir="/tmp"
    ))
    env = clean_test_environment(owned)
    env["PYTHONPATH"] = os.pathsep.join([str(ROOT / "backend/tests"), str(ROOT / "backend")])
    selected = [row for row in manifest["test_files"] if row["path"] in set(plan["selected_files"]) and row["primary_level"] in set(plan["required_levels"]) - {"L0"}]
    hosted_profiles = {"qualified-child-process", "hosted-isolated-compose"}
    deferred_paths = {
        row["path"] for row in selected
        if getattr(args, "defer_hosted", False) and row.get("execution_profile") in hosted_profiles
    }
    blocked: list[str] = []
    node_status: dict[str, str] = {}
    executions: list[dict[str, Any]] = []
    failure_diagnostics: list[dict[str, Any]] = []
    try:
        l0_started = time.monotonic()
        l0_ok, l0_problems = run_l0(plan["changed_paths"], owned, plan["base_commit"])
        l0_elapsed = round(time.monotonic() - l0_started, 3)
        # Required native suites never become green through a pytest skip.
        for row in selected:
            if row["path"] in deferred_paths:
                continue
            if row.get("execution_profile") in {"qualified-child-process", "hosted-isolated-compose"}:
                profile = row["execution_profile"]
                message = ("hosted isolated Compose launcher is not available in the routine gate"
                           if profile == "hosted-isolated-compose" else
                           "requires a qualified child-process profile; none is available in the routine gate")
                blocked.append(f"{row['path']}: {message}")
                if row.get("native") and not args.include_native and (
                    row["path"] in plan.get("required_native_suites", []) or plan.get("full_suite_required")
                ):
                    blocked.append(f"{row['path']}: required native suite needs explicit --include-native planning")
                continue
            if not row.get("native"):
                continue
            if not args.include_native:
                if row["path"] in plan.get("required_native_suites", []) or plan.get("full_suite_required"):
                    blocked.append(f"{row['path']}: required native suite needs explicit --include-native planning")
            elif row["path"].endswith("test_protocol_native_validation.py"):
                binary = args.mihomo_binary
                if not binary or not Path(binary).is_file():
                    blocked.append(f"{row['path']}: pinned Mihomo binary missing; provide --mihomo-binary")
                else:
                    check = subprocess.run([binary, "-v"], capture_output=True, text=True, timeout=10, check=False, env=env)
                    if check.returncode or not check.stdout.startswith("Mihomo Meta v1.19.31 "):
                        blocked.append(f"{row['path']}: pinned Mihomo v1.19.31 verification failed")
                    else:
                        env["FWROUTER_TEST_MIHOMO_BINARY"] = str(Path(binary).resolve())
            elif row["path"].endswith("test_xray_native_readback.py"):
                image = args.xray_image
                if not image or not image.startswith("sha256:"):
                    blocked.append(f"{row['path']}: immutable local Xray image ID missing; provide --xray-image sha256:…")
                elif shutil.which("docker", path=env["PATH"]) is None:
                    blocked.append(f"{row['path']}: Docker is not present for local isolated Xray readback")
                else:
                    check = subprocess.run(["docker", "image", "inspect", image], capture_output=True, timeout=10, check=False, env=env)
                    if check.returncode:
                        blocked.append(f"{row['path']}: immutable Xray image is not already present")
                    else:
                        env["FWROUTER_XRAY_TEST_IMAGE"] = image
        for row in selected:
            path = row["path"]
            if path in deferred_paths:
                executions.append({
                    "suite": row["id"], "path": path,
                    "primary_level": row["primary_level"],
                    "status": "deferred_remote_hosted",
                    "elapsed_seconds": 0.0,
                    "fixture_identity": row["fixture_identity"],
                    "fixture_identity_sha256": hashlib.sha256(row["fixture_identity"].encode()).hexdigest(),
                    "fixture_owner": row["fixture_owner"],
                    "execution_profile": row["execution_profile"],
                })
                continue
            if row.get("execution_profile") in {"qualified-child-process", "hosted-isolated-compose"}:
                executions.append({"suite": row["id"], "path": path,
                                   "primary_level": row["primary_level"],
                                   "status": "blocked_missing_execution_profile",
                                   "elapsed_seconds": 0.0,
                                   "fixture_identity": row["fixture_identity"],
                                   "fixture_identity_sha256": hashlib.sha256(row["fixture_identity"].encode()).hexdigest(),
                                   "fixture_owner": row["fixture_owner"]})
                continue
            if row.get("native") and any(path in reason for reason in blocked):
                executions.append({"suite": row["id"], "path": path, "primary_level": row["primary_level"], "status": "blocked_missing_required_dependency", "elapsed_seconds": 0.0, "fixture_identity": row["fixture_identity"], "fixture_identity_sha256": hashlib.sha256(row["fixture_identity"].encode()).hexdigest(), "fixture_owner": row["fixture_owner"]})
                continue
            suite_env = dict(env)
            node_report_path: Path | None = None
            if path.startswith("backend/tests/") or path.startswith("integrations/") or path.startswith("installer/tests/"):
                rel = path.removeprefix("backend/")
                suite_root, reports_root = create_suite_root(owned, row["id"])
                report_id = hashlib.sha256(row["id"].encode("utf-8")).hexdigest()[:16]
                node_report_path = reports_root / f"{report_id}.nodes.json"
                suite_env["FWROUTER_PYTEST_COORDINATOR_ROOT"] = str(suite_root.resolve())
                suite_env["FWROUTER_GATE_NODE_REPORT"] = str(node_report_path)
                argv = [sys.executable, "-m", "pytest", "-p", "gate_plugin", "-p", "no:cacheprovider", "--basetemp", str(suite_root / "pytest-tmp"), rel]
                if plan.get("manual_subset"):
                    argv.extend(["-m", f"level(value='{plan['subset_selector']['level']}')"])
                cwd = ROOT / "backend" if path.startswith("backend/") else ROOT
                # Both integration and installer pytest files need backend modules.
                if cwd == ROOT:
                    suite_env["PYTHONPATH"] = os.pathsep.join([str(ROOT / "backend/tests"), str(ROOT / "backend")])
            elif path.startswith("ui/"):
                argv = ["node", "--test", "--test-reporter=tap", path]
                cwd = ROOT
            elif path == "installer/test-install.sh":
                argv = ["sh", path]
                cwd = ROOT
            elif path in {"tests/gates/test_acceptance_contract.py", "tests/gates/test_gate_contract.py",
                          "tests/gates/test_smoke_contract.py"}:
                argv = [sys.executable, path]
                cwd = ROOT
            else:
                blocked.append(f"{path}: no reviewed runner adapter")
                executions.append({"suite": row["id"], "path": path, "status": "blocked_no_runner"})
                continue
            max_output = row["max_output_bytes"]
            suite_started = time.monotonic()
            code, output, truncated = run_process(argv, row["timeout_seconds"], max_output, cwd=cwd, env=suite_env)
            suite_elapsed = round(time.monotonic() - suite_started, 3)
            node_report_size = (
                node_report_path.stat().st_size
                if node_report_path is not None and node_report_path.exists() else 0
            )
            artifact_exceeded = node_report_size > row["max_artifact_bytes"]
            if path.startswith("backend/tests/") or path.startswith("integrations/") or path.startswith("installer/tests/"):
                assert node_report_path is not None
                observed = {} if artifact_exceeded else node_status_from_plugin(node_report_path, row["max_artifact_bytes"])
            elif path.startswith("ui/"):
                observed = node_status_from_tap(output, path)
            else:
                observed = {f"{path}::{row['id']}": "passed" if code == 0 else "failed"}
            node_status.update(observed)
            status = "passed" if code == 0 and not truncated and not artifact_exceeded and observed and all(s == "passed" for s in observed.values()) else "failed"
            executions.append({"suite": row["id"], "path": path, "primary_level": row["primary_level"], "status": status, "exit_code": code, "elapsed_seconds": suite_elapsed, "output_bytes_captured": len(output.encode()), "output_limit_exceeded": truncated, "node_report_bytes": node_report_size, "report_limit_exceeded": artifact_exceeded, "node_count": len(observed), "fixture_identity": row["fixture_identity"], "fixture_identity_sha256": hashlib.sha256(row["fixture_identity"].encode()).hexdigest(), "fixture_owner": row["fixture_owner"], "command": argv})
            if status != "passed":
                blocked.append(f"{path}: command or test result failed/was incomplete")
                if len(failure_diagnostics) < 64:
                    failure_diagnostics.append({
                        "path": path,
                        "suite": row["id"],
                        "exit_code": code,
                        "output_limit_exceeded": truncated,
                        "node_report_limit_exceeded": artifact_exceeded,
                        "failed_or_skipped_node_ids": sorted(
                            node for node, node_result in observed.items()
                            if node_result != "passed"
                        )[:256],
                        "captured_output_redacted": redact_failure_output(output),
                    })
        completed_levels = sorted({row["primary_level"] for row in selected if any(entry["path"] == row["path"] and entry["status"] == "passed" for entry in executions)})
        if l0_ok:
            completed_levels.insert(0, "L0")
        else:
            blocked.extend(l0_problems)
        result = {
            "schema_version": 1,
            "evidence_scope": "manual_subset" if plan.get("manual_subset") else ("L6_full_suite" if plan.get("full_suite_required") else "affected_change"),
            "deploy_eligibility": False if plan.get("manual_subset") else None,
            "source_commit": plan["source_commit"],
            "baseline_commit": None,
            "manifest_version": manifest["version"],
            "manifest_digest": plan["manifest_digest"],
            "tool_version": TOOL_VERSION,
            "required_execution_profiles": plan["required_execution_profiles"],
            "quick_deferred_profile_suites": plan.get("quick_deferred_profile_suites", []),
            "deferred_remote_suites": sorted(deferred_paths),
            "execution_complete": not deferred_paths,
            "available_execution_profiles": [],
            "environment": environment_evidence(env),
            "plan_digest": plan["plan_digest"],
            "completed_levels": completed_levels,
            "l0": {"status": "passed" if l0_ok else "failed", "elapsed_seconds": l0_elapsed, "checks": ["git diff --check", "changed-source syntax"], "problems": l0_problems},
            "executions": [{"suite": "L0-static", "path": "@L0", "status": "passed" if l0_ok else "failed", "primary_level": "L0", "elapsed_seconds": l0_elapsed}, *executions],
            "node_status": node_status,
            "blocked_reasons": blocked,
            "failure_diagnostics": failure_diagnostics,
            "eligible": False,
            "raw_output_retained": False,
            "redacted_failure_output_retained": bool(failure_diagnostics),
            "runner_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        }
        try:
            baseline_path = Path(args.baseline) if args.baseline else ROOT / manifest["baseline_policy_file"]
            baseline = load_baseline(baseline_path)
            result["baseline_commit"] = baseline["baseline_commit"]
            classification = classify(plan, result, baseline)
            result["classification"] = classification
            result["eligible"] = (
                not blocked
                and classification["eligible"]
                and set(plan["required_levels"]) <= set(completed_levels)
            )
        except (GateError, OSError, ValueError, KeyError) as exc:
            result["blocked_reasons"].append(f"baseline classification unavailable: {type(exc).__name__}")
        result["report_digest"] = canonical_digest(result)
        if args.output:
            atomic_json(Path(args.output), result)
        print_json(result)
        # A deferred unit leg may finish successfully so a workflow aggregator
        # can collect the independent hosted receipt.  It can never establish
        # eligibility: the report contains non-passing deferred suite rows and
        # promote() rejects them.  Any actual test/profile failure stays red.
        if result["eligible"]:
            return 0
        if getattr(args, "defer_hosted", False) and deferred_paths and not blocked:
            by_path = {entry.get("path"): entry for entry in executions}
            executed_paths = set(by_path) - deferred_paths
            execution_complete = (
                l0_ok
                and executed_paths == set(plan["selected_files"]) - deferred_paths
                and all(by_path[path].get("status") == "passed" for path in executed_paths)
            )
            classification = result.get("classification")
            if not isinstance(classification, dict):
                classification = {}
            deferred_ids = {
                node for node in classification.get("baseline_failures_skipped_or_uncollected", [])
                if canonical_test_path(node.split("::", 1)[0]) in deferred_paths
            }
            classification_ok_except_deferred = (
                isinstance(classification, dict)
                and classification.get("source_commit") == plan.get("source_commit")
                and classification.get("plan_digest") == plan.get("plan_digest")
                and set(classification.get("selected_files", [])) == set(plan.get("selected_files", []))
                and not classification.get("novel_failures")
                and not classification.get("baseline_failures_unapproved")
                and set(classification.get("baseline_failures_skipped_or_uncollected", [])) == deferred_ids
            )
            if (execution_complete and classification_ok_except_deferred
                    and not result.get("blocked_reasons")):
                return 0
        return 1
    finally:
        shutil.rmtree(owned, ignore_errors=True)


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    sub = result.add_subparsers(dest="command", required=True)
    validate = sub.add_parser("validate")
    validate.add_argument("--manifest", default=str(MANIFEST))
    validate.set_defaults(func=command_validate)
    plan = sub.add_parser("plan")
    plan.add_argument("--base", required=True)
    plan.add_argument("--paths", nargs="*")
    plan.add_argument("--include-native", action="store_true")
    plan.add_argument("--quick-push", action="store_true",
                      help="affected-domain fast lane: run only selected L1 suites plus L0")
    plan.add_argument("--output")
    plan.add_argument("--manifest", default=str(MANIFEST))
    plan.set_defaults(func=command_plan)
    subset = sub.add_parser("subset-plan")
    subset.add_argument("--domain")
    subset.add_argument("--level", choices=["L1", "L2", "L3", "L4"])
    subset.add_argument("--include-native", action="store_true")
    subset.add_argument("--output")
    subset.add_argument("--manifest", default=str(MANIFEST))
    subset.set_defaults(func=command_subset_plan_entry)
    dry = sub.add_parser("dry-run")
    dry.add_argument("--level", choices=sorted(LEVELS), required=True)
    dry.add_argument("--manual", action="store_true")
    dry.add_argument("--manifest", default=str(MANIFEST))
    dry.set_defaults(func=command_dry_run)
    classify_parser = sub.add_parser("classify")
    classify_parser.add_argument("--plan", required=True)
    classify_parser.add_argument("--execution", required=True)
    classify_parser.add_argument("--baseline", required=True)
    classify_parser.add_argument("--manifest", default=str(MANIFEST))
    classify_parser.add_argument("--output")
    classify_parser.set_defaults(func=command_classify)
    promote_parser = sub.add_parser("promotion-check")
    promote_parser.add_argument("--plan", required=True)
    promote_parser.add_argument("--report", dest="reports", action="append", default=[])
    promote_parser.add_argument("--manifest", default=str(MANIFEST))
    promote_parser.add_argument("--output")
    promote_parser.set_defaults(func=command_promote)
    smoke = sub.add_parser("smoke")
    smoke.add_argument("--profile", choices=["isolated", "live-readonly"], required=True)
    smoke.add_argument("--reason")
    smoke.add_argument("--opt-in", action="store_true")
    smoke.add_argument("--api-base-url")
    smoke.add_argument("--db-path")
    smoke.add_argument("--validators-json")
    smoke.add_argument("--failed-units-allowlist-json")
    smoke.add_argument("--complete-native", action="store_true")
    smoke.set_defaults(func=command_smoke)
    run = sub.add_parser("run")
    run.add_argument("--plan", required=True)
    run.add_argument("--manifest", default=str(MANIFEST))
    run.add_argument("--baseline")
    run.add_argument("--output")
    run.add_argument("--manual-full-suite", action="store_true")
    run.add_argument("--manual-subset", action="store_true")
    run.add_argument("--include-native", action="store_true")
    run.add_argument("--defer-hosted", action="store_true",
                     help="defer qualified profiles to a separate hosted executor; report remains ineligible")
    run.add_argument("--mihomo-binary")
    run.add_argument("--xray-image")
    run.set_defaults(func=command_run)
    full = sub.add_parser("full-suite-plan")
    full.add_argument("--manual", action="store_true")
    full.add_argument("--manifest", default=str(MANIFEST))
    full.add_argument("--output")
    full.add_argument("--include-native", action="store_true")
    full.set_defaults(func=command_full_suite_plan)
    return result


def main() -> int:
    args = parser().parse_args()
    try:
        return args.func(args)
    except (GateError, OSError, ValueError, KeyError) as exc:
        print(f"gate: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
