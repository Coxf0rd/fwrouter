#!/usr/bin/python3
"""Capture and restore the FWRouter-owned nft/policy-routing apply snapshot.

This helper is intentionally narrow: it only understands the existing
FWRouter table and the fwmark/table-100 policy-routing tuples. Ambiguous state
is rejected before any mutation instead of being guessed at by rollback.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any


TABLE_FAMILY = "inet"
TABLE_NAME = "fwrouter_v2"
ROUTE_TABLE = 100
ROUTE_TABLE_NAME = "fwrouter_vpn"
STATE_SCHEMA = "fwrouter-dataplane-snapshot-state/v1"
MAX_ARTIFACT_BYTES = 8 * 1024 * 1024
MARKS = {"ipv4": ((100, 256), (101, 258)), "ipv6": ((100, 256), (101, 258))}


class SnapshotError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _run(argv: list[str], *, input_bytes: bytes | None = None) -> subprocess.CompletedProcess[bytes]:
    try:
        return subprocess.run(
            argv,
            input=input_bytes,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            timeout=15,
            env=os.environ.copy(),
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise SnapshotError("DATAPLANE_SNAPSHOT_COMMAND_FAILED", f"Command could not complete: {argv[0]}.") from exc


def _json_command(argv: list[str], *, code: str) -> Any:
    result = _run(argv)
    if result.returncode != 0:
        raise SnapshotError(code, f"Read-only command failed: {argv[0]}.")
    try:
        return json.loads(result.stdout)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SnapshotError(code, f"Read-only command returned invalid JSON: {argv[0]}.") from exc


def _table_exists() -> bool:
    payload = _json_command(["nft", "-j", "list", "tables"], code="NFT_TABLES_READ_FAILED")
    if not isinstance(payload, dict) or not isinstance(payload.get("nftables"), list):
        raise SnapshotError("NFT_TABLES_READ_FAILED", "nft table inventory has an unsupported shape.")
    matches = [
        entry.get("table")
        for entry in payload["nftables"]
        if isinstance(entry, dict) and isinstance(entry.get("table"), dict)
        and entry["table"].get("family") == TABLE_FAMILY
        and entry["table"].get("name") == TABLE_NAME
    ]
    if len(matches) > 1:
        raise SnapshotError("NFT_TABLE_STATE_AMBIGUOUS", "The owned nft table is listed more than once.")
    return bool(matches)


def _read_regular(path: Path, *, code: str, missing_ok: bool = False) -> bytes | None:
    _ensure_trusted_parent(path)
    try:
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(path, flags)
    except FileNotFoundError:
        if missing_ok:
            return None
        raise SnapshotError(code, f"Required snapshot file is missing: {path.name}.")
    except OSError as exc:
        raise SnapshotError(code, f"Snapshot file cannot be opened safely: {path.name}.") from exc
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_size > MAX_ARTIFACT_BYTES
                or info.st_uid != os.geteuid() or info.st_mode & 0o022
                or info.st_nlink != 1):
            raise SnapshotError(code, f"Snapshot file owner, mode, type, link count, or size is unsafe: {path.name}.")
        chunks: list[bytes] = []
        remaining = MAX_ARTIFACT_BYTES + 1
        while remaining:
            block = os.read(fd, min(65536, remaining))
            if not block:
                break
            chunks.append(block)
            remaining -= len(block)
        data = b"".join(chunks)
        if len(data) > MAX_ARTIFACT_BYTES:
            raise SnapshotError(code, f"Snapshot file exceeds the size limit: {path.name}.")
        return data
    finally:
        os.close(fd)


def _ensure_trusted_parent(path: Path) -> None:
    try:
        parent = path.parent.resolve(strict=True)
        info = parent.stat()
        if (parent != path.parent.absolute() or not stat.S_ISDIR(info.st_mode)
                or info.st_uid != os.geteuid() or info.st_mode & 0o022):
            raise SnapshotError("SNAPSHOT_PATH_UNSAFE", f"Snapshot parent path is not canonical: {path.parent}.")
    except OSError as exc:
        raise SnapshotError("SNAPSHOT_PATH_UNSAFE", f"Snapshot parent path is unavailable: {path.parent}.") from exc


def _ensure_safe_destination(path: Path) -> None:
    _ensure_trusted_parent(path)
    if path.exists() or path.is_symlink():
        raise SnapshotError("SNAPSHOT_ARTIFACT_EXISTS", f"Snapshot artifact already exists: {path.name}.")


def _write_new_durable(path: Path, data: bytes) -> None:
    _ensure_safe_destination(path)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    tmp = Path(tmp_name)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb", closefd=True) as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        # link() gives O_EXCL-like no-clobber publication of the completed file.
        os.link(tmp, path, follow_symlinks=False)
        tmp.unlink()
        dir_fd = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    except Exception:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise


def _parse_table_vpn_requirement(snapshot: bytes | None, present: bool) -> bool:
    if not present:
        return False
    assert snapshot is not None
    text = snapshot.decode("utf-8", errors="strict")
    if not re.search(r"(?m)^\s*table\s+inet\s+fwrouter_v2\s*\{", text):
        raise SnapshotError("NFT_SNAPSHOT_IDENTITY_INVALID", "Snapshot does not declare the owned table.")
    required = bool(re.search(r"fwrouter (?:global vpn contract|vpn policy contract required) v1", text))
    return required


def _parse_table_mode(snapshot: bytes | None, present: bool) -> str | None:
    if not present or snapshot is None:
        return None
    text = snapshot.decode("utf-8", errors="strict")
    markers = {
        "direct": 'comment "global direct v1"',
        "vpn": 'comment "global vpn v1"',
        "selective": 'comment "selective direct IPv4"',
    }
    matches = [mode for mode, marker in markers.items() if marker in text]
    if len(matches) != 1:
        raise SnapshotError("NFT_SNAPSHOT_MODE_AMBIGUOUS", "Previous owned table lacks one recognized routing-mode marker.")
    return matches[0]


def _as_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        try:
            return int(value, 0) if value.lower().startswith("0x") else int(value)
        except ValueError:
            return None
    return None


def _table_id(value: Any) -> bool:
    return _as_int(value) == ROUTE_TABLE or value == ROUTE_TABLE_NAME


def _capture_rules(family: str) -> list[dict[str, Any]]:
    ip_family = "-4" if family == "ipv4" else "-6"
    payload = _json_command(["ip", "-j", ip_family, "rule", "show"], code="IP_RULE_READ_FAILED")
    if not isinstance(payload, list):
        raise SnapshotError("IP_RULE_READ_FAILED", f"{family} rule inventory has an unsupported shape.")
    selected: list[dict[str, int]] = []
    seen: set[tuple[int, int]] = set()
    supported_keys = {"priority", "src", "dst", "table", "fwmark", "fwmask", "mark", "mask", "protocol"}
    for rule in payload:
        if not isinstance(rule, dict):
            raise SnapshotError("IP_RULE_READ_FAILED", f"{family} rule entry has an unsupported shape.")
        if not _table_id(rule.get("table")):
            continue
        raw_mark = rule.get("fwmark", rule.get("mark"))
        inline_mask: int | None = None
        if isinstance(raw_mark, str) and "/" in raw_mark:
            raw_mark, raw_mask = raw_mark.split("/", 1)
            inline_mask = _as_int(raw_mask)
        mark = _as_int(raw_mark)
        priority = _as_int(rule.get("priority"))
        if mark not in {256, 258} or priority is None or (priority, mark) not in MARKS[family]:
            raise SnapshotError("IP_RULE_STATE_UNSUPPORTED", f"{family} contains a noncanonical rule for table 100.")
        if set(rule) - supported_keys:
            raise SnapshotError("IP_RULE_STATE_UNSUPPORTED", f"{family} table-100 rule has unsupported selectors.")
        if rule.get("src", "all") != "all" or rule.get("dst", "all") != "all":
            raise SnapshotError("IP_RULE_STATE_UNSUPPORTED", f"{family} table-100 rule has source or destination selectors.")
        mask = _as_int(rule.get("fwmask", rule.get("mask")))
        if inline_mask is not None and mask is not None and inline_mask != mask:
            raise SnapshotError("IP_RULE_STATE_UNSUPPORTED", f"{family} table-100 rule reports conflicting masks.")
        mask = inline_mask if inline_mask is not None else mask
        if mask not in {None, 0xFFFFFFFF}:
            raise SnapshotError("IP_RULE_STATE_UNSUPPORTED", f"{family} table-100 rule has a noncanonical mask.")
        protocol = rule.get("protocol")
        if protocol not in {None, "unspec", 0, "static", "kernel", "boot"}:
            raise SnapshotError("IP_RULE_STATE_UNSUPPORTED", f"{family} table-100 rule has an unsupported protocol.")
        key = (priority, mark)
        if key in seen:
            raise SnapshotError("IP_RULE_STATE_UNSUPPORTED", f"{family} has duplicate table-100 rules.")
        seen.add(key)
        selected.append({
            "priority": priority,
            "fwmark": mark,
            "fwmask": mask if mask is not None else 0xFFFFFFFF,
            "protocol": protocol,
        })
    return sorted(selected, key=lambda item: (item["priority"], item["fwmark"]))


def _capture_local_route(family: str) -> dict[str, Any] | None:
    ip_family = "-4" if family == "ipv4" else "-6"
    payload = _json_command(
        # Query the full FIB: some iproute2/kernel combinations return an
        # error for a not-yet-created table even though that state is valid.
        # Filter explicitly so unrelated routes are observed but never owned.
        ["ip", "-j", ip_family, "route", "show", "table", "all"],
        code="IP_ROUTE_READ_FAILED",
    )
    if not isinstance(payload, list):
        raise SnapshotError("IP_ROUTE_READ_FAILED", f"{family} table-100 route inventory has an unsupported shape.")
    found = 0
    allowed_keys = {"type", "dst", "dev", "table", "protocol", "scope", "flags", "prefsrc"}
    expected_dst = "default" if family == "ipv4" else "::/0"
    captured: dict[str, Any] | None = None
    for route in payload:
        if not isinstance(route, dict):
            raise SnapshotError("IP_ROUTE_READ_FAILED", f"{family} route entry has an unsupported shape.")
        # iproute2 omits `table` on main-table entries. Those and all other
        # tables are outside this helper's ownership; only table 100 is read
        # or restored by this snapshot contract.
        if not _table_id(route.get("table")):
            continue
        # The query is scoped to table 100. Any route other than the exact local
        # intercept route is outside this helper's ownership and blocks mutation.
        if route.get("type") != "local" or route.get("dev") != "lo" or route.get("dst") not in {expected_dst, "0.0.0.0/0" if family == "ipv4" else expected_dst}:
            raise SnapshotError("IP_ROUTE_STATE_UNSUPPORTED", f"{family} table 100 contains a noncanonical route.")
        if set(route) - allowed_keys:
            raise SnapshotError("IP_ROUTE_STATE_UNSUPPORTED", f"{family} table-100 route has unsupported fields.")
        if "table" in route and not _table_id(route["table"]):
            raise SnapshotError("IP_ROUTE_STATE_UNSUPPORTED", f"{family} route reports an unexpected table.")
        if route.get("flags") not in (None, [], ()) or route.get("prefsrc") not in {None, ""}:
            raise SnapshotError("IP_ROUTE_STATE_UNSUPPORTED", f"{family} local route has unsupported flags or preferred source.")
        protocol = route.get("protocol")
        if protocol not in {None, "unspec", 0, "static", "kernel", "boot"}:
            raise SnapshotError("IP_ROUTE_STATE_UNSUPPORTED", f"{family} local route has unsupported protocol.")
        scope = route.get("scope")
        if scope not in {None, "host"}:
            raise SnapshotError("IP_ROUTE_STATE_UNSUPPORTED", f"{family} local route has unsupported scope.")
        captured = {
            "type": "local",
            "dst": route["dst"],
            "dev": "lo",
            "table": ROUTE_TABLE,
            "protocol": protocol,
            "scope": scope,
        }
        found += 1
    if found > 1:
        raise SnapshotError("IP_ROUTE_STATE_UNSUPPORTED", f"{family} contains duplicate local routes in table 100.")
    return captured if found == 1 else None


def _capture_routing() -> dict[str, Any]:
    return {
        "ipv4_rules": _capture_rules("ipv4"),
        "ipv6_rules": _capture_rules("ipv6"),
        "ipv4_local_default_route": _capture_local_route("ipv4"),
        "ipv6_local_default_route": _capture_local_route("ipv6"),
    }


def _snapshot_state(snapshot_path: Path, state_path: Path, *, require_absent: bool) -> tuple[dict[str, Any], bytes | None]:
    # A missing state receipt has a distinct fail-closed result. In particular,
    # it must never be inferred from the presence/absence of the nft snapshot.
    state_raw = _read_regular(state_path, code="NFT_SNAPSHOT_STATE_INVALID", missing_ok=True)
    if state_raw is None:
        raise SnapshotError("NFT_SNAPSHOT_STATE_MISSING", "Previous owned-table state metadata is missing.")
    try:
        state = json.loads(state_raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SnapshotError("NFT_SNAPSHOT_STATE_INVALID", "Previous owned-table state metadata is invalid JSON.") from exc
    expected_keys = {
        "schema", "previous_table_state", "snapshot_sha256", "previous_table_vpn_policy_required",
        "previous_routing_mode", "routing",
    }
    if not isinstance(state, dict) or set(state) != expected_keys or state.get("schema") != STATE_SCHEMA:
        raise SnapshotError("NFT_SNAPSHOT_STATE_INVALID", "Previous owned-table state metadata has an unsupported schema.")
    previous = state.get("previous_table_state")
    if previous not in {"present", "missing"}:
        raise SnapshotError("NFT_SNAPSHOT_STATE_INVALID", "Previous owned-table presence is not explicit.")
    snapshot = _read_regular(snapshot_path, code="NFT_ROLLBACK_SNAPSHOT_INVALID", missing_ok=previous == "missing")
    if previous == "present":
        if not snapshot:
            raise SnapshotError("NFT_ROLLBACK_SNAPSHOT_MISSING", "Previous owned-table snapshot is empty or missing.")
        digest = hashlib.sha256(snapshot).hexdigest()
        if state.get("snapshot_sha256") != digest:
            raise SnapshotError("NFT_ROLLBACK_SNAPSHOT_HASH_MISMATCH", "Previous owned-table snapshot does not match its state receipt.")
        _parse_table_vpn_requirement(snapshot, True)
        if state.get("previous_routing_mode") != _parse_table_mode(snapshot, True):
            raise SnapshotError("NFT_SNAPSHOT_STATE_INVALID", "Captured routing mode does not match the snapshot.")
        if state.get("previous_table_vpn_policy_required") != _parse_table_vpn_requirement(snapshot, True):
            raise SnapshotError("NFT_SNAPSHOT_STATE_INVALID", "Captured VPN requirement does not match the snapshot.")
    elif snapshot is not None or state.get("snapshot_sha256") is not None or state.get("previous_table_vpn_policy_required") is not False or state.get("previous_routing_mode") is not None:
        raise SnapshotError("NFT_SNAPSHOT_STATE_INVALID", "Missing-table receipt unexpectedly references a prior table.")
    if not isinstance(state.get("previous_table_vpn_policy_required"), bool):
        raise SnapshotError("NFT_SNAPSHOT_STATE_INVALID", "Previous VPN policy requirement is invalid.")
    routing = state.get("routing")
    if not _valid_routing_state(routing):
        raise SnapshotError("NFT_SNAPSHOT_STATE_INVALID", "Captured policy-routing state is invalid or unsupported.")
    return state, snapshot


def _valid_routing_state(value: Any) -> bool:
    if not isinstance(value, dict) or set(value) != {
        "ipv4_rules", "ipv6_rules", "ipv4_local_default_route", "ipv6_local_default_route"
    }:
        return False
    for family in ("ipv4", "ipv6"):
        rules = value.get(f"{family}_rules")
        if not isinstance(rules, list):
            return False
        seen: set[tuple[int, int]] = set()
        for entry in rules:
            if not isinstance(entry, dict) or set(entry) != {"priority", "fwmark", "fwmask", "protocol"}:
                return False
            priority, mark, mask = entry.get("priority"), entry.get("fwmark"), entry.get("fwmask")
            if not isinstance(priority, int) or not isinstance(mark, int) or (priority, mark) not in MARKS[family] or mask != 0xFFFFFFFF:
                return False
            if entry.get("protocol") not in {None, "unspec", 0, "static", "kernel", "boot"}:
                return False
            if (priority, mark) in seen:
                return False
            seen.add((priority, mark))
    for family in ("ipv4", "ipv6"):
        route = value.get(f"{family}_local_default_route")
        if route is not None:
            if not isinstance(route, dict) or set(route) != {"type", "dst", "dev", "table", "protocol", "scope"}:
                return False
            if route["type"] != "local" or route["dev"] != "lo" or route["table"] != ROUTE_TABLE:
                return False
            expected = {"default", "0.0.0.0/0"} if family == "ipv4" else {"::/0"}
            if route["dst"] not in expected or route["protocol"] not in {None, "unspec", 0, "static", "kernel", "boot"} or route["scope"] not in {None, "host"}:
                return False
    return True


def _table_bytes(path: Path) -> bytes:
    data = _read_regular(path, code="NFT_CANDIDATE_INVALID")
    assert data is not None
    try:
        _parse_table_vpn_requirement(data, True)
    except SnapshotError as exc:
        raise SnapshotError("NFT_CANDIDATE_IDENTITY_INVALID", "Candidate does not declare the FWRouter-owned table.") from exc
    return data


def _batch(table_present: bool, new_table: bytes | None) -> bytes:
    pieces: list[bytes] = []
    if table_present:
        pieces.append(b"delete table inet fwrouter_v2\n")
    if new_table is not None:
        pieces.append(new_table.rstrip() + b"\n")
    return b"\n".join(pieces)


def _run_nft_batch(batch: bytes) -> None:
    if not batch:
        return
    checked = _run(["nft", "-c", "-f", "-"], input_bytes=batch)
    if checked.returncode != 0:
        raise SnapshotError("NFT_BATCH_VALIDATION_FAILED", "Atomic nft batch failed native validation; live table was not changed.")
    applied = _run(["nft", "-f", "-"], input_bytes=batch)
    if applied.returncode != 0:
        raise SnapshotError("NFT_BATCH_APPLY_FAILED", "Atomic nft batch failed; nftables rejected the transaction.")


def _apply_candidate(candidate_path: Path, snapshot_path: Path, state_path: Path) -> dict[str, Any]:
    state, _ = _snapshot_state(snapshot_path, state_path, require_absent=False)
    candidate = _table_bytes(candidate_path)
    # Fence external writers between capture and apply. Only the exact captured
    # owned table and policy-routing facts may be replaced by this operation.
    table_present = _table_exists()
    if table_present != (state["previous_table_state"] == "present"):
        raise SnapshotError("NFT_SNAPSHOT_STALE", "Owned table presence changed after snapshot capture.")
    if table_present:
        live = _run(["nft", "-s", "list", "table", TABLE_FAMILY, TABLE_NAME])
        if live.returncode != 0 or hashlib.sha256(live.stdout).hexdigest() != state["snapshot_sha256"]:
            raise SnapshotError("NFT_SNAPSHOT_STALE", "Owned table changed after snapshot capture.")
    if _capture_routing() != state["routing"]:
        raise SnapshotError("IP_ROUTING_SNAPSHOT_STALE", "FWRouter policy-routing facts changed after snapshot capture.")
    _run_nft_batch(_batch(table_present, candidate))
    return state


def _ip_sync_routing(target: dict[str, Any]) -> None:
    for family in ("ipv4", "ipv6"):
        ip_family = "-4" if family == "ipv4" else "-6"
        wanted_entries = {(entry["priority"], entry["fwmark"]): entry for entry in target[f"{family}_rules"]}
        wanted = set(wanted_entries)
        current = _capture_rules(family)
        for entry in current:
            key = (entry["priority"], entry["fwmark"])
            if key not in wanted or entry != wanted_entries[key]:
                _run(["ip", ip_family, "rule", "del", "priority", str(key[0]), "fwmark", hex(key[1]), "table", str(ROUTE_TABLE)])
        current_route = _capture_local_route(family)
        wanted_route = target[f"{family}_local_default_route"]
        if current_route is not None and current_route != wanted_route:
            route = ["ip", ip_family, "route", "del", "local", current_route["dst"], "dev", current_route["dev"], "table", str(current_route["table"])]
            if current_route["scope"]:
                route.extend(["scope", current_route["scope"]])
            if current_route["protocol"] not in {None, "unspec", 0}:
                route.extend(["proto", str(current_route["protocol"])])
            result = _run(route)
            if result.returncode != 0:
                raise SnapshotError("IP_ROUTING_RESTORE_FAILED", f"Could not remove the current FWRouter-owned {family} local route.")
            current_route = None
        if wanted_route is not None and current_route != wanted_route:
            route = ["ip", ip_family, "route", "replace", "local", wanted_route["dst"], "dev", wanted_route["dev"], "table", str(wanted_route["table"])]
            if wanted_route["scope"]:
                route.extend(["scope", wanted_route["scope"]])
            if wanted_route["protocol"] not in {None, "unspec", 0}:
                route.extend(["proto", str(wanted_route["protocol"])])
            result = _run(route)
            if result.returncode != 0:
                raise SnapshotError("IP_ROUTING_RESTORE_FAILED", f"Could not restore the captured {family} local route.")
        present = {(entry["priority"], entry["fwmark"]) for entry in _capture_rules(family)}
        for priority, mark in sorted(wanted - present):
            wanted_rule = wanted_entries[(priority, mark)]
            route_mark = hex(mark) if wanted_rule["fwmask"] == 0xFFFFFFFF else f"{hex(mark)}/{hex(wanted_rule['fwmask'])}"
            argv = ["ip", ip_family, "rule", "add", "priority", str(priority), "fwmark", route_mark, "table", str(ROUTE_TABLE)]
            if wanted_rule["protocol"] not in {None, "unspec", 0}:
                argv.extend(["protocol", str(wanted_rule["protocol"])])
            result = _run(argv)
            if result.returncode != 0:
                raise SnapshotError("IP_ROUTING_RESTORE_FAILED", f"Could not restore the captured {family} fwmark rule.")
    # Re-read the exact owner-scoped contract after writes. Unknown state remains
    # an error; no broad table/rule cleanup is ever used here.
    if _capture_routing() != target:
        raise SnapshotError("IP_ROUTING_RESTORE_VERIFY_FAILED", "Restored FWRouter policy-routing facts do not match the snapshot.")


def capture(snapshot_path: Path, state_path: Path) -> str:
    _ensure_safe_destination(snapshot_path)
    _ensure_safe_destination(state_path)
    present = _table_exists()
    snapshot: bytes | None = None
    if present:
        # `-s` omits volatile stateful counter values. The recovery artifact is
        # a ruleset snapshot, not a promise to restore packet counters.
        result = _run(["nft", "-s", "list", "table", TABLE_FAMILY, TABLE_NAME])
        if result.returncode != 0:
            raise SnapshotError("NFT_SNAPSHOT_CAPTURE_FAILED", "Owned table exists but its exact snapshot could not be read.")
        snapshot = result.stdout
        _parse_table_vpn_requirement(snapshot, True)
    routing = _capture_routing()
    state = {
        "schema": STATE_SCHEMA,
        "previous_table_state": "present" if present else "missing",
        "snapshot_sha256": hashlib.sha256(snapshot).hexdigest() if snapshot is not None else None,
        "previous_table_vpn_policy_required": _parse_table_vpn_requirement(snapshot, True) if present else False,
        "previous_routing_mode": _parse_table_mode(snapshot, True) if present else None,
        "routing": routing,
    }
    state_bytes = (json.dumps(state, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
    if snapshot is not None:
        _write_new_durable(snapshot_path, snapshot)
    _write_new_durable(state_path, state_bytes)
    return state["previous_table_state"]


def rollback(snapshot_path: Path, state_path: Path) -> dict[str, Any]:
    state, snapshot = _snapshot_state(snapshot_path, state_path, require_absent=False)
    # Validate the current supported owner-scoped IP shape before the nft batch
    # changes anything. Other host rules/routes are never selected or touched.
    _capture_routing()
    present = _table_exists()
    target_present = state["previous_table_state"] == "present"
    if target_present:
        assert snapshot is not None
        _run_nft_batch(_batch(present, snapshot))
    elif present:
        _run_nft_batch(_batch(True, None))
    _ip_sync_routing(state["routing"])
    final_present = _table_exists()
    if final_present != target_present:
        raise SnapshotError("NFT_ROLLBACK_VERIFY_FAILED", "Owned table presence does not match the captured previous state.")
    if target_present:
        assert snapshot is not None
        live = _run(["nft", "-s", "list", "table", TABLE_FAMILY, TABLE_NAME])
        if live.returncode != 0:
            raise SnapshotError("NFT_ROLLBACK_VERIFY_FAILED", "Restored owned table could not be read back.")
        if live.stdout.rstrip() != snapshot.rstrip():
            raise SnapshotError("NFT_ROLLBACK_VERIFY_FAILED", "Restored owned table does not match the captured stateless snapshot.")
        try:
            live_vpn_required = _parse_table_vpn_requirement(live.stdout, True)
            live_mode = _parse_table_mode(live.stdout, True)
        except SnapshotError as exc:
            raise SnapshotError("NFT_ROLLBACK_VERIFY_FAILED", "Restored owned table has invalid identity or routing markers.") from exc
        if live_vpn_required != state["previous_table_vpn_policy_required"] or live_mode != state["previous_routing_mode"]:
            raise SnapshotError("NFT_ROLLBACK_VERIFY_FAILED", "Restored owned table does not match captured routing identity.")
    return state


def main(argv: list[str]) -> int:
    operation = "rollback" if len(argv) > 1 and argv[1] == "rollback" else "apply"
    stage = "rollback" if operation == "rollback" else ("apply" if len(argv) > 1 and argv[1] == "apply-candidate" else "snapshot")
    try:
        if len(argv) == 4 and argv[1] == "capture":
            previous = capture(Path(argv[2]), Path(argv[3]))
            print(previous)
            return 0
        if len(argv) == 5 and argv[1] == "apply-candidate":
            state = _apply_candidate(Path(argv[2]), Path(argv[3]), Path(argv[4]))
            print(state["previous_table_state"])
            return 0
        if len(argv) == 4 and argv[1] == "rollback":
            state = rollback(Path(argv[2]), Path(argv[3]))
            previous = state["previous_table_state"]
            restored = previous == "present"
            print(json.dumps({
                "ok": True,
                "operation": "rollback",
                "stage": "rollback",
                "adapter": "nft-owned-table",
                "dataplane_capability": "nft_owned_table",
                "capability": "nft_owned_table",
                "enforcement_level": "owned_table_ready",
                "traffic_enforcement_guaranteed": False,
                "owned_table": "inet fwrouter_v2",
                "routing_mode": state["previous_routing_mode"],
                "restored_previous_table": restored,
                "previous_table_state": previous,
                "table_exists": restored,
                "vpn_policy_required": state["previous_table_vpn_policy_required"],
                "vpn_external_path_verified": False,
                "vpn_fwmark_hex": "0x00000100",
                "message": "Previous FWRouter owned-table and captured policy-routing state restored.",
            }, separators=(",", ":")))
            return 0
        print("usage: dataplane-snapshot-state.py {capture SNAPSHOT STATE|apply-candidate CANDIDATE SNAPSHOT STATE|rollback SNAPSHOT STATE}", file=sys.stderr)
        return 64
    except SnapshotError as exc:
        print(json.dumps({
            "ok": False,
            "operation": operation,
            "stage": stage,
            "adapter": "nft-owned-table",
            "error_code": exc.code,
            "message": str(exc),
        }, separators=(",", ":")))
        return 1
    except (OSError, ValueError, UnicodeError) as exc:
        print(json.dumps({
            "ok": False,
            "operation": operation,
            "stage": stage,
            "adapter": "nft-owned-table",
            "error_code": "DATAPLANE_SNAPSHOT_IO_FAILED",
            "message": str(exc),
        }, separators=(",", ":")))
        return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
