"""pytest metadata plugin used by tests/gates/gate.py (test code only)."""
from __future__ import annotations

import json
import importlib.util
import os
from pathlib import Path
from typing import Any

import pytest

_NODE_STATUSES: dict[str, str] = {}
_COLLECTED_NODEIDS: list[str] = []
_NODE_EXECUTIONS: dict[str, dict[str, Any]] = {}
_EXACT_NODEIDS: dict[str, str] = {}
_AUTHORIZED_SKIP_REASONS: dict[str, dict[str, Any]] = {}
_INJECTED_SKIP_REASONS: dict[str, str] = {}
_REVIEWED_SKIP_POLICIES: dict[str, dict[str, Any]] = {}
_NODE_OVERRIDE_FIELDS = frozenset({"path", "node", "primary_level", "execution_profile"})
# `execution_profile` is merged only when the override contains that key;
# explicit null clears the file profile, while absence preserves inheritance.


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _manifest() -> dict[str, Any]:
    path = Path(os.environ.get("FWROUTER_GATE_MANIFEST", _repo_root() / "tests/gates/manifest.json"))
    data = json.loads(path.read_text(encoding="utf-8"))
    return data


def _reviewed_skip_policies(data: dict[str, Any]) -> dict[str, dict[str, Any]]:
    gate_path = _repo_root() / "tests/gates/gate.py"
    spec = importlib.util.spec_from_file_location("_fwrouter_gate_policy_registry", gate_path)
    if spec is None or spec.loader is None:
        raise pytest.UsageError("cannot load source-owned node skip registry")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    try:
        module.validate_manifest(data)
    except Exception as exc:
        raise pytest.UsageError(f"gate manifest or reviewed node skip registry is invalid: {exc}") from exc
    return dict(module.SKIP_POLICY_REGISTRY)


def _normalized_node(nodeid: str) -> str:
    # Parametrized suffixes are removed only for metadata lookup. Result and
    # baseline records always keep pytest's exact raw node ID.
    before, separator, after = nodeid.partition("::")
    if not separator:
        return nodeid
    tail = after.split("::")
    tail[-1] = tail[-1].split("[", 1)[0]
    return before + "::" + "::".join(tail)


def _root_relative(item: pytest.Item) -> str:
    return Path(item.path).resolve().relative_to(_repo_root()).as_posix()


def _exact_repo_nodeid(nodeid: str) -> str:
    # The raw pytest path may be relative to either the repository root or
    # backend/. Resolve it from the actual collected Item.path, never by
    # guessing from a string prefix. Unknown IDs remain unknown and therefore
    # fail the exact selected/collected/executed coverage checks downstream.
    return _EXACT_NODEIDS.get(nodeid, nodeid)


def _merged_node_metadata(base_row: dict[str, Any], override: dict[str, Any] | None) -> dict[str, Any]:
    """Apply partial node overrides while retaining required file-level metadata."""
    metadata = dict(base_row)
    if override is not None:
        unknown = sorted(set(override) - _NODE_OVERRIDE_FIELDS)
        if unknown:
            raise ValueError("gate node override has unknown field(s): " + ", ".join(unknown))
        metadata.update({key: value for key, value in override.items() if key not in {"path", "node"}})
    missing = [
        field for field in ("primary_level", "domain")
        if not isinstance(metadata.get(field), str) or not metadata[field].strip()
    ]
    if missing:
        raise ValueError("gate metadata missing required field(s): " + ", ".join(missing))
    return metadata


def pytest_sessionstart(session: pytest.Session) -> None:
    _NODE_STATUSES.clear()
    _COLLECTED_NODEIDS.clear()
    _NODE_EXECUTIONS.clear()
    _EXACT_NODEIDS.clear()
    _AUTHORIZED_SKIP_REASONS.clear()
    _INJECTED_SKIP_REASONS.clear()


def pytest_addoption(parser: pytest.Parser) -> None:
    group = parser.getgroup("fwrouter-manual-host-smoke")
    group.addoption("--fwrouter-manual-host-smoke", action="store_true", default=False,
                    help="explicitly run the registered manual live-readonly host smoke")
    group.addoption("--fwrouter-host-smoke-reason", default=None)
    group.addoption("--fwrouter-host-smoke-api-base-url", default=None)
    group.addoption("--fwrouter-host-smoke-db-path", default=None)
    group.addoption("--fwrouter-host-smoke-validators-json", default=None)
    group.addoption("--fwrouter-host-smoke-failed-units-allowlist-json", default=None)


def pytest_collection_finish(session: pytest.Session) -> None:
    # Capture the exact same-session inventory, including parametrized suffixes.
    for item in session.items:
        _, separator, tail = item.nodeid.partition("::")
        exact = _root_relative(item) + (separator + tail if separator else "")
        _EXACT_NODEIDS[item.nodeid] = exact
        _COLLECTED_NODEIDS.append(exact)


def pytest_runtest_logstart(nodeid: str, location: tuple[str, int | None, str]) -> None:
    exact = _exact_repo_nodeid(nodeid)
    row = _NODE_EXECUTIONS.setdefault(exact, {"start_count": 0, "call_report_count": 0,
                                               "terminal_report_count": 0, "status": None})
    row["start_count"] += 1


def pytest_runtest_logreport(report: pytest.TestReport) -> None:
    nodeid = _exact_repo_nodeid(report.nodeid)
    execution = _NODE_EXECUTIONS.setdefault(nodeid, {"start_count": 0, "call_report_count": 0,
                                                       "terminal_report_count": 0, "status": None})
    previous = _NODE_STATUSES.get(nodeid)
    if report.failed:
        _NODE_STATUSES[nodeid] = "failed"
        execution["status"] = "failed"
        if report.when in {"setup", "call"}:
            execution["terminal_report_count"] += 1
    elif report.skipped and report.when in {"setup", "call"}:
        if previous != "failed":
            _NODE_STATUSES[nodeid] = "skipped"
            execution["status"] = "skipped"
        execution["terminal_report_count"] += 1
        actual_reason = _actual_skip_reason(report)
        execution["skip_reason"] = actual_reason
        injected_reason = _INJECTED_SKIP_REASONS.get(nodeid)
        if injected_reason is not None and actual_reason == injected_reason:
            policy = _REVIEWED_SKIP_POLICIES[nodeid]
            _AUTHORIZED_SKIP_REASONS[nodeid] = {**policy, "pytest_reason": actual_reason}
    elif report.when == "call" and report.passed and previous is None:
        _NODE_STATUSES[nodeid] = "passed"
        execution["status"] = "passed"
        execution["terminal_report_count"] += 1
    if report.when == "call":
        execution["call_report_count"] += 1


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    destination = os.environ.get("FWROUTER_GATE_NODE_REPORT")
    if not destination:
        return
    path = Path(destination)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_version": 2,
        "collected_nodeids": _COLLECTED_NODEIDS,
        "node_status": dict(sorted(_NODE_STATUSES.items())),
        "node_executions": dict(sorted(_NODE_EXECUTIONS.items())),
        "authorized_skip_reasons": dict(sorted(_AUTHORIZED_SKIP_REASONS.items())),
    }
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
    temporary.replace(path)


def pytest_configure(config: pytest.Config) -> None:
    for marker, description in (
        ("level(value)", "one primary test level, L0-L4 or L7"),
        ("domain(value)", "test ownership domain from the checked gate catalog"),
        ("native", "uses a local native executable or isolated local container"),
        ("slow", "requires an explicit slow-test selection"),
        ("network", "uses an explicitly controlled network boundary"),
        ("destructive", "destructive test; requires disposable staging attestation"),
        ("live", "live system test; forbidden in routine CI"),
    ):
        config.addinivalue_line("markers", f"{marker}: {description}")
    if config.getoption("--fwrouter-manual-host-smoke"):
        if os.environ.get("GITHUB_ACTIONS", "").lower() == "true":
            raise pytest.UsageError("manual host smoke opt-in is forbidden in GitHub Actions")
        required = ("--fwrouter-host-smoke-reason", "--fwrouter-host-smoke-api-base-url",
                    "--fwrouter-host-smoke-db-path", "--fwrouter-host-smoke-validators-json",
                    "--fwrouter-host-smoke-failed-units-allowlist-json")
        absent = [name for name in required if not config.getoption(name)]
        if absent:
            raise pytest.UsageError("manual host smoke opt-in requires: " + ", ".join(absent))


def _actual_skip_reason(report: pytest.TestReport) -> str:
    longrepr = report.longrepr
    if isinstance(longrepr, tuple) and len(longrepr) >= 3:
        value = str(longrepr[2])
    else:
        value = str(longrepr)
    return value.removeprefix("Skipped: ").strip()


def _policy_reason(policy: dict[str, Any]) -> str:
    return f"FWROUTER_SKIP_POLICY[{policy['reason_code']}]: {policy['disposition']}"


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    data = _manifest()
    reviewed_policies = _reviewed_skip_policies(data)
    _REVIEWED_SKIP_POLICIES.clear()
    _REVIEWED_SKIP_POLICIES.update(reviewed_policies)
    rows = {row["path"]: row for row in data["test_files"]}
    overrides = {(row["path"], row["node"]): row for row in data.get("node_overrides", [])}
    unsafe: list[str] = []
    manual_host_enabled = config.getoption("--fwrouter-manual-host-smoke")
    policies = {row["nodeid"]: row for row in data.get("node_skip_policies", [])}
    for item in items:
        path = _root_relative(item)
        row = rows.get(path)
        if row is None:
            unsafe.append(f"unclassified pytest item: {item.nodeid}")
            continue
        exact_id = _root_relative(item) + "::" + item.nodeid.partition("::")[2]
        policy = policies.get(exact_id)
        if policy is not None:
            expected_policy = reviewed_policies.get(exact_id)
            if expected_policy is None or any(policy.get(key) != value for key, value in expected_policy.items()):
                unsafe.append(f"node skip policy differs from the source-owned registry: {exact_id}")
                continue
            if policy["reason_code"] == "retired_qemu":
                policy_value = dict(reviewed_policies[exact_id])
                _INJECTED_SKIP_REASONS[exact_id] = _policy_reason(policy_value)
                item.add_marker(pytest.mark.skip(reason=_INJECTED_SKIP_REASONS[exact_id]))
            elif not manual_host_enabled:
                policy_value = dict(reviewed_policies[exact_id])
                _INJECTED_SKIP_REASONS[exact_id] = _policy_reason(policy_value)
                item.add_marker(pytest.mark.skip(reason=_INJECTED_SKIP_REASONS[exact_id]))
        base_node = _normalized_node(item.nodeid)
        # The catalog stores node keys relative to the file, e.g. `::test_x`.
        relative_node = base_node.split("::", 1)[1] if "::" in base_node else ""
        override = overrides.get((path, relative_node))
        try:
            metadata = _merged_node_metadata(row, override)
        except ValueError as exc:
            unsafe.append(f"unclassified pytest item: {item.nodeid}: {exc}")
            continue
        level = metadata["primary_level"]
        item.add_marker(pytest.mark.level(value=level))
        item.add_marker(pytest.mark.domain(value=metadata["domain"]))
        if metadata.get("native"):
            item.add_marker(pytest.mark.native)
        if metadata.get("slow") is True:
            item.add_marker(pytest.mark.slow)
        if metadata.get("network") == "controlled":
            item.add_marker(pytest.mark.network)
        if metadata.get("destructive"):
            item.add_marker(pytest.mark.destructive)
        if item.get_closest_marker("live_dataplane") or item.get_closest_marker("live"):
            unsafe.append(f"live test denied; no staging-attestation verifier is installed: {item.nodeid}")
        if item.get_closest_marker("destructive") or metadata.get("destructive"):
            unsafe.append(f"destructive test denied; no staging-attestation verifier is installed: {item.nodeid}")
    if unsafe:
        raise pytest.UsageError("\n".join(unsafe))
