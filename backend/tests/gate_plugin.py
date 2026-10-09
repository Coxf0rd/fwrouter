"""pytest metadata plugin used by tests/gates/gate.py (test code only)."""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest

_NODE_STATUSES: dict[str, str] = {}
_NODE_OVERRIDE_FIELDS = frozenset({"path", "node", "primary_level"})


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _manifest() -> dict[str, Any]:
    path = Path(os.environ.get("FWROUTER_GATE_MANIFEST", _repo_root() / "tests/gates/manifest.json"))
    data = json.loads(path.read_text(encoding="utf-8"))
    return data


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
    path, separator, tail = nodeid.partition("::")
    if path.startswith("tests/") and not path.startswith("tests/gates/"):
        path = "backend/" + path
    return path + (separator + tail if separator else "")


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


def pytest_runtest_logreport(report: pytest.TestReport) -> None:
    nodeid = _exact_repo_nodeid(report.nodeid)
    previous = _NODE_STATUSES.get(nodeid)
    if report.failed:
        _NODE_STATUSES[nodeid] = "failed"
    elif report.skipped and report.when in {"setup", "call"}:
        if previous != "failed":
            _NODE_STATUSES[nodeid] = "skipped"
    elif report.when == "call" and report.passed and previous is None:
        _NODE_STATUSES[nodeid] = "passed"


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    destination = os.environ.get("FWROUTER_GATE_NODE_REPORT")
    if not destination:
        return
    path = Path(destination)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"schema_version": 1, "node_status": dict(sorted(_NODE_STATUSES.items()))}
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


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    data = _manifest()
    rows = {row["path"]: row for row in data["test_files"]}
    overrides = {(row["path"], row["node"]): row for row in data.get("node_overrides", [])}
    unsafe: list[str] = []
    for item in items:
        path = _root_relative(item)
        row = rows.get(path)
        if row is None:
            unsafe.append(f"unclassified pytest item: {item.nodeid}")
            continue
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
