from __future__ import annotations

import importlib.util
import json
import os
import re
import signal
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any

import pytest


REPO = Path(__file__).resolve().parents[2]
HOST_SCRIPTS = REPO / "host/libexec/fwrouter"
HELPER_PATH = HOST_SCRIPTS / "dataplane-snapshot-state.py"
_spec = importlib.util.spec_from_file_location("dataplane_snapshot_state", HELPER_PATH)
assert _spec and _spec.loader
HELPER = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(HELPER)


def _table(mode: str, marker: str | None = None) -> bytes:
    extra = f' comment "{marker}"' if marker else ""
    return (
        "table inet fwrouter_v2 {\n"
        f'    chain fwrouter_classify {{ comment "global {mode} v1"{extra}\n'
        "    }\n"
        "}\n"
    ).encode()


def _nft_payload_for_table(table: bytes) -> dict[str, Any]:
    text = table.decode("utf-8")
    comments = re.findall(r'comment "([^"]+)"', text)
    return {"nftables": [
        {"table": {"family": "inet", "name": "fwrouter_v2", "handle": 1}},
        {"chain": {"family": "inet", "table": "fwrouter_v2", "name": "fwrouter_classify",
                    "handle": 2, "comment": comments[0] if comments else ""}},
        {"rule": {"family": "inet", "table": "fwrouter_v2", "chain": "fwrouter_classify",
                   "handle": 3, "expr": [{"comment": comment} for comment in comments]}},
    ]}


def _table_id_for_test(value: Any) -> bool:
    return value in (100, "100", "fwrouter_vpn")


class NativeFixture:
    """In-process adapter for the helper's read/write native command contract."""

    def __init__(self, *, table: bytes | None = None) -> None:
        self.table = table
        self.rules: dict[str, list[dict[str, Any]]] = {"ipv4": [], "ipv6": []}
        self.routes: dict[str, list[dict[str, Any]]] = {"ipv4": [], "ipv6": []}
        self.mutations: list[list[str]] = []
        self.fail_rule_add = False
        self.fail_nft_validation = False
        self.fail_nft_apply = False

    def run(self, argv: list[str], *, input_bytes: bytes | None = None):
        import subprocess

        result = subprocess.CompletedProcess(argv, 0, stdout=b"", stderr=b"")
        if argv[:3] == ["nft", "-j", "list"] and argv[3:] == ["tables"]:
            entries = [] if self.table is None else [{"table": {"family": "inet", "name": "fwrouter_v2"}}]
            result.stdout = json.dumps({"nftables": entries}).encode()
        elif argv in (["nft", "list", "table", "inet", "fwrouter_v2"],
                      ["nft", "-s", "list", "table", "inet", "fwrouter_v2"]):
            if self.table is None:
                result.returncode = 1
            else:
                result.stdout = self.table
        elif argv == ["nft", "-j", "-s", "list", "table", "inet", "fwrouter_v2"]:
            if self.table is None:
                result.returncode = 1
            else:
                result.stdout = json.dumps(_nft_payload_for_table(self.table)).encode()
        elif argv in (["nft", "-c", "-f", "-"], ["nft", "-f", "-"]):
            batch = input_bytes or b""
            if argv[1] == "-c":
                if self.fail_nft_validation or b"INVALID" in batch:
                    result.returncode = 1
                    result.stderr = b"synthetic nft validation failure"
                return result
            if self.fail_nft_apply:
                result.returncode = 1
                result.stderr = b"synthetic nft apply failure"
                return result
            self.mutations.append(argv)
            if batch.startswith(b"delete table inet fwrouter_v2"):
                self.table = None
            start = batch.find(b"table inet fwrouter_v2 {")
            if start >= 0:
                self.table = batch[start:].rstrip() + b"\n"
        elif argv[0] == "ip":
            family = "ipv4" if "-4" in argv else "ipv6"
            if argv[:2] == ["ip", "-j"] and argv[3:5] == ["rule", "show"]:
                result.stdout = json.dumps(self.rules[family]).encode()
            elif argv[:2] == ["ip", "-j"] and argv[3:5] == ["route", "show"]:
                result.stdout = json.dumps(self.routes[family]).encode()
            elif "rule" in argv and "del" in argv:
                self.mutations.append(argv)
                priority = int(argv[argv.index("priority") + 1])
                mark = int(argv[argv.index("fwmark") + 1], 0)
                before = len(self.rules[family])
                self.rules[family] = [
                    rule for rule in self.rules[family]
                    if not (
                        int(rule.get("priority", -1)) == priority
                        and int(rule.get("fwmark", rule.get("mark", -1))) == mark
                        and int(rule.get("table", -1)) == 100
                    )
                ]
                if len(self.rules[family]) == before:
                    result.returncode = 2
            elif "rule" in argv and "add" in argv:
                self.mutations.append(argv)
                if self.fail_rule_add:
                    result.returncode = 2
                else:
                    rule = {
                        "priority": int(argv[argv.index("priority") + 1]),
                        "fwmark": int(argv[argv.index("fwmark") + 1], 0), "table": 100,
                    }
                    if "protocol" in argv:
                        rule["protocol"] = argv[argv.index("protocol") + 1]
                    self.rules[family].append(rule)
            elif "route" in argv and "replace" in argv:
                self.mutations.append(argv)
                route = {"type": "local", "dst": argv[argv.index("local") + 1], "dev": "lo", "table": 100}
                if "scope" in argv:
                    route["scope"] = argv[argv.index("scope") + 1]
                if "metric" in argv:
                    route["metric"] = int(argv[argv.index("metric") + 1])
                if "pref" in argv:
                    route["pref"] = argv[argv.index("pref") + 1]
                if "proto" in argv:
                    route["protocol"] = argv[argv.index("proto") + 1]
                self.routes[family] = [item for item in self.routes[family] if not (
                    _table_id_for_test(item.get("table")) and item.get("type") == "local"
                    and item.get("dst") in {"default", "0.0.0.0/0", "::/0"}
                )] + [route]
            elif "route" in argv and "del" in argv:
                self.mutations.append(argv)
                dst = argv[argv.index("local") + 1]
                before = len(self.routes[family])
                self.routes[family] = [route for route in self.routes[family] if not (
                    route.get("dst") == dst and _table_id_for_test(route.get("table"))
                )]
                if len(self.routes[family]) == before:
                    result.returncode = 2
            else:
                result.returncode = 64
                result.stderr = b"unsupported synthetic ip command"
        else:
            result.returncode = 64
            result.stderr = b"unsupported synthetic native command"
        return result


def _fixture(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    table: bytes | None = None,
    rules4: list[dict] | None = None,
    rules6: list[dict] | None = None,
    routes4: list[dict] | None = None,
    routes6: list[dict] | None = None,
) -> tuple[NativeFixture, Path, Path, Path]:
    native = NativeFixture(table=table)
    native.rules = {"ipv4": rules4 or [], "ipv6": rules6 or []}
    native.routes = {"ipv4": routes4 or [], "ipv6": routes6 or []}
    monkeypatch.setattr(HELPER, "_run", native.run)
    return native, tmp_path / "snapshot.nft", tmp_path / "snapshot-state.json", tmp_path / "candidate.nft"


def _candidate(path: Path, mode: str = "direct", marker: str | None = None) -> None:
    path.write_bytes(_table(mode, marker))


def _raises_code(code: str, call) -> None:
    with pytest.raises(HELPER.SnapshotError) as caught:
        call()
    assert caught.value.code == code


def test_present_snapshot_is_durable_before_atomic_apply_and_restores_actual_owner_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    old = _table("vpn", "fwrouter vpn policy contract required v1")
    old_rule4 = [
        {"priority": 100, "fwmark": 256, "table": 100},
        {"priority": 101, "fwmark": 258, "table": 100},
        {"priority": 32766, "src": "all", "table": "main"},
    ]
    old_rule6 = [{"priority": 100, "fwmark": 256, "table": 100}]
    old_route4 = [
        {"type": "unicast", "dst": "192.0.2.0/24", "dev": "eth0", "table": "main"},
        {"type": "local", "dst": "default", "dev": "lo", "table": 100, "protocol": "static",
         "scope": "host", "metric": None, "pref": None},
    ]
    old_route6 = [{"type": "local", "dst": "::/0", "dev": "lo", "table": 100,
                   "scope": "host", "metric": None, "pref": None}]
    native, snapshot, state, candidate = _fixture(
        monkeypatch, tmp_path, table=old, rules4=old_rule4, rules6=old_rule6,
        routes4=old_route4, routes6=old_route6,
    )
    _candidate(candidate, "direct")

    assert HELPER.capture(snapshot, state) == "present"
    receipt = json.loads(state.read_text(encoding="utf-8"))
    assert receipt["previous_table_state"] == "present"
    assert receipt["snapshot_sha256"]
    assert receipt["routing"]["ipv4_rules"] == [
        {"priority": 100, "fwmark": 256, "fwmask": 0xFFFFFFFF, "protocol": None},
        {"priority": 101, "fwmark": 258, "fwmask": 0xFFFFFFFF, "protocol": None},
    ]
    assert HELPER._apply_candidate(candidate, snapshot, state)["previous_table_state"] == "present"
    assert native.table == _table("direct")
    assert HELPER.rollback(snapshot, state)["previous_table_state"] == "present"
    assert native.table == old
    assert native.rules["ipv4"] == old_rule4
    assert native.rules["ipv6"] == old_rule6
    assert native.routes["ipv4"] == old_route4
    assert native.routes["ipv6"] == old_route6
    assert len([argv for argv in native.mutations if argv[:2] == ["nft", "-f"]]) == 2


@pytest.mark.parametrize("receipt_state", ["missing", "corrupt", "hash-mismatch", "invalid-schema", "duplicate-key"])
def test_rollback_rejects_missing_or_invalid_receipt_before_native_mutation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, receipt_state: str,
) -> None:
    native, snapshot, state, _ = _fixture(monkeypatch, tmp_path, table=_table("direct"))
    if receipt_state != "missing":
        if receipt_state == "duplicate-key":
            HELPER.capture(snapshot, state)
            original = state.read_text(encoding="utf-8").rstrip("\n")
            state.write_text(original[:-1] + ',"previous_table_state":"missing"}' + "\n", encoding="utf-8")
        else:
            snapshot.write_bytes(_table("vpn", "fwrouter vpn policy contract required v1"))
        if receipt_state == "corrupt":
            state.write_text("{", encoding="utf-8")
        elif receipt_state == "hash-mismatch":
            state.write_text(json.dumps({"schema": "fwrouter-dataplane-snapshot-state/v2", "previous_table_state": "present"}), encoding="utf-8")
        else:
            state.write_text(json.dumps({"previous_table_state": "present"}), encoding="utf-8")
    before = (native.table, list(native.mutations))
    _raises_code("NFT_SNAPSHOT_STATE_MISSING" if receipt_state == "missing" else "NFT_SNAPSHOT_STATE_INVALID", lambda: HELPER.rollback(snapshot, state))
    assert (native.table, native.mutations) == before


def test_capture_write_precondition_failure_does_not_mutate_native_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    native, snapshot, state, _ = _fixture(monkeypatch, tmp_path, table=_table("direct"))
    state.write_text("owner-preexisting-file", encoding="utf-8")
    before = (native.table, list(native.mutations))
    _raises_code("SNAPSHOT_ARTIFACT_EXISTS", lambda: HELPER.capture(snapshot, state))
    assert (native.table, native.mutations) == before
    assert not snapshot.exists()


def test_capture_rejects_group_world_writable_snapshot_parent_before_native_reads_or_writes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    native, snapshot, state, _ = _fixture(monkeypatch, tmp_path, table=_table("direct"))
    tmp_path.chmod(0o777)
    try:
        _raises_code("SNAPSHOT_PATH_UNSAFE", lambda: HELPER.capture(snapshot, state))
        assert native.mutations == []
        assert not snapshot.exists() and not state.exists()
    finally:
        tmp_path.chmod(0o700)


def test_rollback_rejects_group_world_writable_state_before_native_mutation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    native, snapshot, state, candidate = _fixture(monkeypatch, tmp_path, table=_table("direct"))
    _candidate(candidate)
    HELPER.capture(snapshot, state)
    HELPER._apply_candidate(candidate, snapshot, state)
    before = (native.table, list(native.mutations))
    state.chmod(0o666)
    try:
        _raises_code("NFT_SNAPSHOT_STATE_INVALID", lambda: HELPER.rollback(snapshot, state))
        assert (native.table, native.mutations) == before
    finally:
        state.chmod(0o600)


def test_invalid_candidate_batch_keeps_previous_table_after_snapshot_capture(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    old = _table("direct")
    native, snapshot, state, candidate = _fixture(monkeypatch, tmp_path, table=old)
    _candidate(candidate, "vpn", "fwrouter vpn policy contract required v1")
    candidate.write_bytes(b"INVALID\n" + candidate.read_bytes())
    assert HELPER.capture(snapshot, state) == "present"
    native.fail_nft_validation = True
    _raises_code("NFT_BATCH_VALIDATION_FAILED", lambda: HELPER._apply_candidate(candidate, snapshot, state))
    assert native.table == old
    assert state.exists() and snapshot.exists()


def test_missing_previous_table_is_explicit_and_rollback_restores_missing_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    native, snapshot, state, candidate = _fixture(monkeypatch, tmp_path)
    _candidate(candidate)
    assert HELPER.capture(snapshot, state) == "missing"
    receipt = json.loads(state.read_text(encoding="utf-8"))
    assert receipt["previous_table_state"] == "missing"
    assert receipt["snapshot_sha256"] is None
    assert not snapshot.exists()
    HELPER._apply_candidate(candidate, snapshot, state)
    assert native.table == _table("direct")
    assert HELPER.rollback(snapshot, state)["previous_table_state"] == "missing"
    assert native.table is None


def test_unknown_table_100_route_stops_before_snapshot_or_apply(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    foreign_route = [{"type": "unicast", "dst": "192.0.2.0/24", "dev": "eth0", "table": 100}]
    native, snapshot, state, _ = _fixture(monkeypatch, tmp_path, table=_table("direct"), routes4=foreign_route)
    _raises_code("IP_ROUTE_STATE_UNSUPPORTED", lambda: HELPER.capture(snapshot, state))
    assert native.table == _table("direct")
    assert native.mutations == []
    assert not snapshot.exists() and not state.exists()


def test_rollback_reconciles_exact_owned_rule_and_route_protocols(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    old = _table("direct")
    rule = {"priority": 100, "fwmark": 256, "table": 100, "protocol": "static"}
    route = {"type": "local", "dst": "default", "dev": "lo", "table": 100,
             "protocol": "static", "scope": "host", "metric": None, "pref": None}
    native, snapshot, state, candidate = _fixture(
        monkeypatch, tmp_path, table=old, rules4=[rule], routes4=[route],
    )
    _candidate(candidate)
    HELPER.capture(snapshot, state)
    HELPER._apply_candidate(candidate, snapshot, state)
    native.rules["ipv4"] = [{**rule, "protocol": "kernel"}]
    native.routes["ipv4"] = [{**route, "protocol": "kernel"}]

    HELPER.rollback(snapshot, state)

    assert native.rules["ipv4"] == [rule]
    assert HELPER._capture_local_route("ipv4") == {
        "type": "local", "dst": "default", "dev": "lo", "table": 100,
        "protocol": "static", "scope": "host", "metric": None, "pref": None,
    }


def test_ipv6_default_route_metric_and_preference_are_captured_and_restored_exactly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    old = _table("direct")
    observed = {
        "type": "local", "dst": "default", "dev": "lo", "table": "fwrouter_vpn",
        "scope": "host", "metric": 1024, "pref": "medium",
    }
    native, snapshot, state, candidate = _fixture(
        monkeypatch, tmp_path, table=old, routes6=[observed],
    )
    _candidate(candidate)
    HELPER.capture(snapshot, state)
    captured = json.loads(state.read_text(encoding="utf-8"))["routing"]["ipv6_local_default_route"]
    assert captured == {
        "type": "local", "dst": "default", "dev": "lo", "table": 100,
        "protocol": None, "scope": "host", "metric": 1024, "pref": "medium",
    }

    HELPER._apply_candidate(candidate, snapshot, state)
    native.routes["ipv6"] = []
    HELPER.rollback(snapshot, state)
    assert HELPER._capture_local_route("ipv6") == captured
    restored_route_command = next(
        argv for argv in native.mutations if argv[:4] == ["ip", "-6", "route", "replace"]
    )
    assert restored_route_command[restored_route_command.index("metric") + 1] == "1024"
    assert restored_route_command[restored_route_command.index("pref") + 1] == "medium"


def test_ipv6_default_route_rejects_unrecognized_native_fields_before_writes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    unknown = {
        "type": "local", "dst": "default", "dev": "lo", "table": 100,
        "metric": 1024, "pref": "medium", "expires": 30,
    }
    native, snapshot, state, _ = _fixture(monkeypatch, tmp_path, routes6=[unknown])
    _raises_code("IP_ROUTE_STATE_UNSUPPORTED", lambda: HELPER.capture(snapshot, state))
    assert native.mutations == []
    assert not snapshot.exists() and not state.exists()


def test_nft_restore_validation_failure_is_not_reported_as_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    old = _table("direct")
    native, snapshot, state, candidate = _fixture(monkeypatch, tmp_path, table=old)
    _candidate(candidate, "vpn", "fwrouter vpn policy contract required v1")
    HELPER.capture(snapshot, state)
    HELPER._apply_candidate(candidate, snapshot, state)
    current = native.table
    snapshot.write_bytes(b"INVALID\n" + snapshot.read_bytes())
    _raises_code("NFT_ROLLBACK_SNAPSHOT_HASH_MISMATCH", lambda: HELPER.rollback(snapshot, state))
    assert native.table == current


def test_shell_entrypoints_delegate_snapshot_and_rollback_to_same_helper() -> None:
    apply = (HOST_SCRIPTS / "dataplane-apply.sh").read_text(encoding="utf-8")
    rollback = (HOST_SCRIPTS / "dataplane-rollback.sh").read_text(encoding="utf-8")
    assert '"$SNAPSHOT_HELPER" capture' in apply
    assert '"$SNAPSHOT_HELPER" apply-candidate' in apply
    assert '"$SNAPSHOT_HELPER" rollback' in rollback
    assert "nft delete table inet fwrouter_v2" not in apply + rollback
    assert 'resolve_vpn_policy_required "$SNAPSHOT_PATH"' not in rollback


def test_nft_semantic_fingerprint_ignores_only_volatile_or_unordered_data() -> None:
    base_entries: list[dict[str, Any]] = [
        {"metainfo": {"version": "1.0"}},
        {"table": {"family": "inet", "name": "fwrouter_v2", "handle": 1}},
        {"chain": {"family": "inet", "table": "fwrouter_v2", "name": "first", "handle": 2}},
        {"chain": {"family": "inet", "table": "fwrouter_v2", "name": "second", "handle": 3}},
        {"set": {"family": "inet", "table": "fwrouter_v2", "name": "static_allow",
                  "type": "ipv4_addr", "handle": 4}},
        {"set": {"family": "inet", "table": "fwrouter_v2", "name": "dns_direct_ipv4",
                  "type": "ipv4_addr", "flags": ["timeout"], "handle": 5}},
        {"set": {"family": "inet", "table": "fwrouter_v2", "name": "dns_vpn_ipv4",
                  "type": "ipv4_addr", "flags": ["timeout"], "handle": 9}},
        {"element": {"family": "inet", "table": "fwrouter_v2", "name": "static_allow",
                      "elem": ["192.0.2.1", "192.0.2.2"]}},
        {"element": {"family": "inet", "table": "fwrouter_v2", "name": "dns_direct_ipv4",
                      "elem": ["198.51.100.1"]}},
        {"element": {"family": "inet", "table": "fwrouter_v2", "name": "dns_vpn_ipv4",
                      "elem": ["198.51.100.2"]}},
        {"rule": {"family": "inet", "table": "fwrouter_v2", "chain": "first", "handle": 6,
                   "expr": [{"counter": {"packets": 1, "bytes": 64}}, {"accept": None}]}},
        {"rule": {"family": "inet", "table": "fwrouter_v2", "chain": "first", "handle": 7,
                   "expr": [{"drop": None}]}},
        {"rule": {"family": "inet", "table": "fwrouter_v2", "chain": "second", "handle": 8,
                   "expr": [{"return": None}]}},
        {"counter": {"family": "inet", "table": "fwrouter_v2", "name": "traffic_tx",
                      "packets": 4, "bytes": 256}},
    ]
    baseline = HELPER._nft_semantic_sha256({"nftables": base_entries})
    reordered = [base_entries[0], base_entries[1], base_entries[3], base_entries[2],
                 base_entries[6], base_entries[5], base_entries[4], base_entries[9], base_entries[8],
                 base_entries[7], base_entries[10], base_entries[11], base_entries[12], base_entries[13]]
    reordered[9] = {"element": {"family": "inet", "table": "fwrouter_v2", "name": "static_allow",
                                "elem": ["192.0.2.2", "192.0.2.1"]}}
    assert HELPER._nft_semantic_sha256({"nftables": reordered}) == baseline

    volatile = json.loads(json.dumps({"nftables": base_entries}))
    volatile["nftables"][10]["rule"]["expr"][0]["counter"].update({"packets": 900, "bytes": 8192})
    volatile["nftables"][8]["element"]["elem"] = ["203.0.113.9"]
    volatile["nftables"][9]["element"]["elem"] = ["203.0.113.10"]
    volatile["nftables"][13]["counter"].update({"packets": 901, "bytes": 8193})
    assert HELPER._nft_semantic_sha256(volatile) == baseline

    # The exemption is limited to the two named timeout sets; arbitrary
    # runtime-populated-looking sets remain part of recovery identity.
    unrelated_dynamic = [*base_entries,
                         {"set": {"family": "inet", "table": "fwrouter_v2", "name": "other_runtime_set",
                                  "type": "ipv4_addr", "flags": ["timeout"]}},
                         {"element": {"family": "inet", "table": "fwrouter_v2", "name": "other_runtime_set",
                                       "elem": ["198.51.100.7"]}}]
    changed_unrelated = json.loads(json.dumps({"nftables": unrelated_dynamic}))
    changed_unrelated["nftables"][-1]["element"]["elem"] = ["198.51.100.8"]
    assert HELPER._nft_semantic_sha256(changed_unrelated) != HELPER._nft_semantic_sha256(
        {"nftables": unrelated_dynamic}
    )

    changed_rule_order = list(base_entries)
    changed_rule_order[10], changed_rule_order[11] = changed_rule_order[11], changed_rule_order[10]
    assert HELPER._nft_semantic_sha256({"nftables": changed_rule_order}) != baseline
    changed_static_set = list(base_entries)
    changed_static_set[7] = {"element": {"family": "inet", "table": "fwrouter_v2", "name": "static_allow",
                                         "elem": ["192.0.2.1", "192.0.2.3"]}}
    assert HELPER._nft_semantic_sha256({"nftables": changed_static_set}) != baseline
    unsupported = [*base_entries, {"unexpected": {"family": "inet", "table": "fwrouter_v2"}}]
    with pytest.raises(ValueError, match="unsupported nftables object"):
        HELPER._nft_semantic_sha256({"nftables": unsupported})


def test_rollback_rejects_successful_native_restore_with_different_semantics(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    old = _table("direct")
    native, snapshot, state, candidate = _fixture(monkeypatch, tmp_path, table=old)
    _candidate(candidate, "vpn", "fwrouter vpn policy contract required v1")
    HELPER.capture(snapshot, state)
    HELPER._apply_candidate(candidate, snapshot, state)
    run = native.run

    def altered_restore(argv: list[str], *, input_bytes: bytes | None = None):
        result = run(argv, input_bytes=input_bytes)
        if argv == ["nft", "-f", "-"] and result.returncode == 0:
            native.table = _table("vpn", "fwrouter vpn policy contract required v1")
        return result

    monkeypatch.setattr(HELPER, "_run", altered_restore)
    _raises_code("NFT_ROLLBACK_SEMANTIC_MISMATCH", lambda: HELPER.rollback(snapshot, state))


def _write_executable(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    path.chmod(0o755)


def _shell_fixture(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, table: bytes | None) -> tuple[Path, Path, Path, Path, Path, Path]:
    runtime = tmp_path / "runtime.json"
    log = tmp_path / "mutations.jsonl"
    runtime.write_text(json.dumps({"table": table.decode() if table else None, "rules": [], "routes": []}), encoding="utf-8")
    nft = tmp_path / "bin/nft"
    ip = tmp_path / "bin/ip"
    _write_executable(nft, r'''#!/usr/bin/python3
import json, os, pathlib, re, sys
p=pathlib.Path(os.environ["FWTEST_STATE"]); s=json.loads(p.read_text()); a=sys.argv[1:]
def save(): p.write_text(json.dumps(s,sort_keys=True))
if a == ["-j","list","tables"]:
 print(json.dumps({"nftables": [] if s["table"] is None else [{"table":{"family":"inet","name":"fwrouter_v2"}}]})); raise SystemExit(0)
if a in (["list","table","inet","fwrouter_v2"],["-s","list","table","inet","fwrouter_v2"]):
 if s["table"] is None: raise SystemExit(1)
 sys.stdout.write(s["table"]); raise SystemExit(0)
if a == ["-j","-s","list","table","inet","fwrouter_v2"]:
 if s["table"] is None: raise SystemExit(1)
 comments=re.findall(r'comment "([^"]+)"',s["table"])
 marker=comments[0] if comments else ""
 print(json.dumps({"nftables":[
  {"table":{"family":"inet","name":"fwrouter_v2","handle":1}},
  {"chain":{"family":"inet","table":"fwrouter_v2","name":"fwrouter_classify","handle":2,"comment":marker}},
  {"rule":{"family":"inet","table":"fwrouter_v2","chain":"fwrouter_classify","handle":3,"expr":[{"comment":x} for x in comments]}}]})); raise SystemExit(0)
if a in (["-c","-f","-"],["-f","-"]):
 d=sys.stdin.read()
 if "INVALID" in d: print("synthetic nft validation failure",file=sys.stderr); raise SystemExit(1)
 if a[0] == "-c":
  if os.environ.get("FWTEST_BLOCK_NFT_CHECK") == "1":
   pathlib.Path(os.environ["FWTEST_NFT_READY"]).write_text("ready")
   import time
   while True: time.sleep(1)
  raise SystemExit(0)
 if os.environ.get("FWTEST_FAIL_NFT_APPLY") == "1":
  print("synthetic atomic nft apply failure",file=sys.stderr); raise SystemExit(1)
 with open(os.environ["FWTEST_LOG"],"a") as f: f.write("nft\n")
 s["table"]=None
 m=re.search(r"(?ms)^table inet fwrouter_v2 \{.*?^\}\s*$",d)
 if m: s["table"]=m.group(0)+"\n"
 save(); raise SystemExit(0)
raise SystemExit(64)
''')
    _write_executable(ip, r'''#!/usr/bin/python3
import json, os, pathlib, sys
p=pathlib.Path(os.environ["FWTEST_STATE"]); s=json.loads(p.read_text()); a=sys.argv[1:]
if a[:2] in (["-j","-4"],["-j","-6"]):
 if a[2:] == ["rule","show"]: print(json.dumps(s["rules"])); raise SystemExit(0)
 if a[2:5] == ["route","show","table"]: print(json.dumps(s["routes"])); raise SystemExit(0)
if a[:2] in (["-4","rule"],["-6","rule"]): raise SystemExit(2)
if a[:2] in (["-4","route"],["-6","route"]): raise SystemExit(2)
raise SystemExit(64)
''')
    common = tmp_path / "dataplane-common.sh"
    common.write_text((HOST_SCRIPTS / "dataplane-common.sh").read_text(encoding="utf-8"), encoding="utf-8")
    helper = tmp_path / "dataplane-snapshot-state.py"
    helper.write_bytes(HELPER_PATH.read_bytes())
    helper.chmod(0o755)
    scripts: list[Path] = []
    for name in ("dataplane-apply.sh", "dataplane-rollback.sh"):
        source = (HOST_SCRIPTS / name).read_text(encoding="utf-8")
        source = source.replace("/usr/local/libexec/fwrouter/dataplane-common.sh", str(common))
        source = source.replace("/usr/local/libexec/fwrouter/dataplane-snapshot-state.py", str(helper))
        target = tmp_path / name
        target.write_text(source, encoding="utf-8")
        target.chmod(0o755)
        scripts.append(target)
    monkeypatch.setenv("FWTEST_STATE", str(runtime))
    monkeypatch.setenv("FWTEST_LOG", str(log))
    monkeypatch.setenv("PATH", f"{nft.parent}:/usr/bin:/bin")
    snapshot, state, candidate, manifest = (tmp_path / "snapshot.nft", tmp_path / "snapshot-state.json", tmp_path / "candidate.nft", tmp_path / "manifest.json")
    candidate.write_bytes(_table("direct"))
    manifest.write_text(json.dumps({"summary": {"global_mode": "direct", "requires_vpn_policy_routing": False}}), encoding="utf-8")
    return scripts[0], scripts[1], snapshot, state, candidate, manifest


def _invoke_shell(script: Path, args: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    environment = {
        "PATH": os.environ["PATH"], "HOME": str(cwd), "LC_ALL": "C",
        "FWTEST_STATE": os.environ["FWTEST_STATE"], "FWTEST_LOG": os.environ["FWTEST_LOG"],
    }
    if "FWTEST_FAIL_NFT_APPLY" in os.environ:
        environment["FWTEST_FAIL_NFT_APPLY"] = os.environ["FWTEST_FAIL_NFT_APPLY"]
    # Both native command names must resolve to these disposable stubs in the
    # subprocess. The system PATH is present only for ordinary tools/interpreter.
    for command in ("nft", "ip"):
        resolved = shutil.which(command, path=environment["PATH"])
        if resolved is None or Path(resolved).parent != cwd / "bin":
            raise AssertionError(f"{command} is not pinned to the disposable test stub")
    return subprocess.run(["/bin/sh", str(script), *args], cwd=cwd, env=environment,
                          text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                          timeout=15, check=False)


def test_real_shell_apply_and_rollback_use_owned_stubs_and_restore_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    old = _table("direct").decode()
    apply, rollback, snapshot, state, candidate, manifest = _shell_fixture(tmp_path, monkeypatch, old.encode())
    applied = _invoke_shell(apply, [str(candidate), str(manifest), str(snapshot), str(state)], tmp_path)
    assert applied.returncode == 0, applied.stdout + applied.stderr
    assert json.loads((tmp_path / "runtime.json").read_text())["table"].rstrip() == candidate.read_text().rstrip()
    restored = _invoke_shell(rollback, [str(snapshot), str(state), str(manifest)], tmp_path)
    assert restored.returncode == 0, restored.stdout + restored.stderr
    assert json.loads((tmp_path / "runtime.json").read_text())["table"].rstrip() == old.rstrip()
    assert (tmp_path / "mutations.jsonl").read_text().splitlines() == ["nft", "nft"]


def test_real_shell_rejects_invalid_nft_candidate_without_removing_old_table(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    old = _table("direct").decode()
    apply, _, snapshot, state, candidate, manifest = _shell_fixture(tmp_path, monkeypatch, old.encode())
    candidate.write_bytes(b"INVALID\n" + candidate.read_bytes())
    result = _invoke_shell(apply, [str(candidate), str(manifest), str(snapshot), str(state)], tmp_path)
    assert result.returncode != 0
    assert json.loads(result.stdout)["error_code"] == "NFT_BATCH_VALIDATION_FAILED"
    assert json.loads((tmp_path / "runtime.json").read_text())["table"].rstrip() == old.rstrip()
    assert not (tmp_path / "mutations.jsonl").exists()


@pytest.mark.parametrize("receipt", ["missing", "corrupt"])
def test_real_shell_rollback_fails_closed_on_missing_or_corrupt_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, receipt: str,
) -> None:
    old = _table("direct").decode()
    _, rollback, snapshot, state, _, _ = _shell_fixture(tmp_path, monkeypatch, old.encode())
    snapshot.write_bytes(_table("vpn", "fwrouter vpn policy contract required v1"))
    if receipt == "corrupt":
        state.write_text("{broken", encoding="utf-8")
    before = json.loads((tmp_path / "runtime.json").read_text())
    result = _invoke_shell(rollback, [str(snapshot), str(state)], tmp_path)
    assert result.returncode != 0
    assert json.loads(result.stdout)["ok"] is False
    assert json.loads((tmp_path / "runtime.json").read_text()) == before
    assert not (tmp_path / "mutations.jsonl").exists()


def test_real_shell_captured_present_apply_failure_can_restore_previous_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    old = _table("vpn", "fwrouter vpn policy contract required v1").decode()
    apply, rollback, snapshot, state, candidate, manifest = _shell_fixture(tmp_path, monkeypatch, old.encode())
    monkeypatch.setenv("FWTEST_FAIL_NFT_APPLY", "1")
    failed = _invoke_shell(apply, [str(candidate), str(manifest), str(snapshot), str(state)], tmp_path)
    assert failed.returncode != 0
    assert json.loads(failed.stdout)["error_code"] == "NFT_BATCH_APPLY_FAILED"
    assert json.loads((tmp_path / "runtime.json").read_text())["table"].rstrip() == old.rstrip()
    assert json.loads(state.read_text())["previous_table_state"] == "present"

    monkeypatch.delenv("FWTEST_FAIL_NFT_APPLY")
    restored = _invoke_shell(rollback, [str(snapshot), str(state)], tmp_path)
    assert restored.returncode == 0, restored.stdout + restored.stderr
    assert json.loads(restored.stdout)["restored_previous_table"] is True
    assert json.loads((tmp_path / "runtime.json").read_text())["table"].rstrip() == old.rstrip()


def test_real_shell_missing_previous_table_is_explicit_and_restored_as_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    apply, rollback, snapshot, state, candidate, manifest = _shell_fixture(tmp_path, monkeypatch, None)
    applied = _invoke_shell(apply, [str(candidate), str(manifest), str(snapshot), str(state)], tmp_path)
    assert applied.returncode == 0, applied.stdout + applied.stderr
    receipt = json.loads(state.read_text())
    assert receipt["previous_table_state"] == "missing"
    assert not snapshot.exists()
    assert json.loads((tmp_path / "runtime.json").read_text())["table"] is not None
    restored = _invoke_shell(rollback, [str(snapshot), str(state)], tmp_path)
    assert restored.returncode == 0, restored.stdout + restored.stderr
    assert json.loads(restored.stdout)["previous_table_state"] == "missing"
    assert json.loads((tmp_path / "runtime.json").read_text())["table"] is None


def test_real_shell_interruption_after_durable_capture_preserves_and_restores_old_table(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    old = _table("vpn", "fwrouter vpn policy contract required v1").decode()
    apply, rollback, snapshot, state, candidate, manifest = _shell_fixture(tmp_path, monkeypatch, old.encode())
    ready = tmp_path / "nft-validation-ready"
    environment = {
        "PATH": os.environ["PATH"], "HOME": str(tmp_path), "LC_ALL": "C",
        "FWTEST_STATE": os.environ["FWTEST_STATE"], "FWTEST_LOG": os.environ["FWTEST_LOG"],
        "FWTEST_BLOCK_NFT_CHECK": "1", "FWTEST_NFT_READY": str(ready),
    }
    for command in ("nft", "ip"):
        resolved = shutil.which(command, path=environment["PATH"])
        assert resolved is not None and Path(resolved).parent == tmp_path / "bin"

    process = subprocess.Popen(
        ["/bin/sh", str(apply), str(candidate), str(manifest), str(snapshot), str(state)],
        cwd=tmp_path, env=environment, stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, start_new_session=True,
    )
    try:
        assert os.getpgid(process.pid) == process.pid, "test subprocess must own its process group"
        deadline = time.monotonic() + 8
        while not ready.exists() and process.poll() is None and time.monotonic() < deadline:
            time.sleep(0.05)
        assert ready.exists(), "apply did not reach post-capture native validation stub"
        assert snapshot.is_file() and state.is_file()
        receipt = json.loads(state.read_text(encoding="utf-8"))
        assert receipt["previous_table_state"] == "present"
        os.killpg(process.pid, signal.SIGKILL)
        stdout, stderr = process.communicate(timeout=5)
        assert process.returncode == -signal.SIGKILL, (stdout, stderr)

        # No candidate batch ran before interruption, so the old table remains
        # active; now exercise the actual rollback entrypoint using the same
        # durable receipt and synthetic nft/ip adapters.
        assert json.loads((tmp_path / "runtime.json").read_text())["table"].rstrip() == old.rstrip()
        assert not (tmp_path / "mutations.jsonl").exists()
        restored = _invoke_shell(rollback, [str(snapshot), str(state)], tmp_path)
        assert restored.returncode == 0, restored.stdout + restored.stderr
        assert json.loads(restored.stdout)["restored_previous_table"] is True
        assert json.loads((tmp_path / "runtime.json").read_text())["table"].rstrip() == old.rstrip()
        assert (tmp_path / "mutations.jsonl").read_text().splitlines() == ["nft"]
    finally:
        if process.poll() is None:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.communicate(timeout=5)
