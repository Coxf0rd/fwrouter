from __future__ import annotations

import importlib.util
import json
import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path
import sys
import subprocess
from pathlib import PurePosixPath
from types import SimpleNamespace

import pytest

from fwrouter_api.services import native_proof


REPO_ROOT = Path(__file__).resolve().parents[2]
XRAY_IMAGE_PIN = "sha256:" + "1" * 64
MIHOMO_IMAGE_PIN = "sha256:" + "6" * 64


def _installer_manifest_module():
    path = REPO_ROOT / "installer" / "controlled_backend_lifecycle.py"
    spec = importlib.util.spec_from_file_location("controlled_backend_lifecycle_for_manifest_test", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_backend_manifest_matches_rollout_owner_and_exclusions(tmp_path: Path) -> None:
    root = tmp_path / "backend"
    root.mkdir()
    (root / "package.py").write_text("important", encoding="utf-8")
    (root / "tool").write_text("binary", encoding="utf-8")
    (root / "tool").chmod(0o755)

    lifecycle = _installer_manifest_module()
    expected = lifecycle._manifest_digest(lifecycle._backend_manifest(root))

    assert native_proof.backend_manifest(root) == lifecycle._backend_manifest(root)
    assert native_proof.backend_manifest_sha256(root) == expected
    excluded_names = (".env", ".env.production", ".venv/module.py", ".pytest_cache/state", "__pycache__/module.py", "cache.db")
    for name in excluded_names:
        relative = Path(name).as_posix()
        assert native_proof._excluded_backend_path(PurePosixPath(relative))
        assert lifecycle._excluded(PurePosixPath(relative))


def test_backend_manifest_rejects_symlink_directory(tmp_path: Path) -> None:
    root = tmp_path / "backend"
    outside = tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    (outside / "module.py").write_text("x", encoding="utf-8")
    (root / "linked").symlink_to(outside, target_is_directory=True)

    with pytest.raises(ValueError, match="BACKEND_SYMLINK_DIRECTORY"):
        native_proof.backend_manifest(root)


def test_readonly_database_sets_query_only_and_does_not_change_database(tmp_path: Path) -> None:
    path = tmp_path / "state.db"
    creator = sqlite3.connect(path)
    creator.execute("CREATE TABLE proof (value TEXT)")
    creator.execute("INSERT INTO proof VALUES ('before')")
    creator.commit()
    creator.close()
    before = path.read_bytes()

    connection = native_proof._open_readonly_database(path)
    try:
        assert connection.in_transaction
        assert connection.execute("PRAGMA query_only").fetchone()[0] == 1
        assert connection.execute("SELECT value FROM proof").fetchone()[0] == "before"
        with pytest.raises(sqlite3.OperationalError):
            connection.execute("UPDATE proof SET value='after'")
    finally:
        connection.rollback()
        connection.close()

    assert path.read_bytes() == before


def test_nested_projection_uses_readonly_connection_and_blocks_expired_ttl_write(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    import stat
    from contextlib import nullcontext

    from fwrouter_api.db import connection as db_connection
    from fwrouter_api.services import server_state

    path = tmp_path / "state.db"
    creator = sqlite3.connect(path)
    creator.executescript("""
        CREATE TABLE routing_global_state (
            id INTEGER PRIMARY KEY, desired_mode TEXT, applied_mode TEXT, selective_default TEXT,
            server_mode TEXT, desired_fixed_server_id TEXT, applied_fixed_server_id TEXT,
            fixed_server_until TEXT, active_auto_server_id TEXT, apply_state TEXT,
            error_code TEXT, error_message TEXT, updated_at TEXT
        );
        CREATE TABLE settings (key TEXT PRIMARY KEY, value_json TEXT NOT NULL);
        INSERT INTO routing_global_state VALUES
            (1, 'vpn', 'vpn', 'direct', 'fixed', 'server-a', 'server-a',
             '2000-01-01 00:00:00', NULL, 'clean', NULL, NULL, '2000-01-01 00:00:00');
        INSERT INTO settings VALUES ('routing.auto_selection_revision', '4');
    """)
    creator.commit()
    creator.close()
    path.chmod(0o640)
    tmp_path.chmod(0o750)
    before_bytes = path.read_bytes()
    before_db_mode = stat.S_IMODE(path.stat().st_mode)
    before_parent_mode = stat.S_IMODE(tmp_path.stat().st_mode)
    before_db_mtime = path.stat().st_mtime_ns
    before_parent_mtime = tmp_path.stat().st_mtime_ns
    monkeypatch.setattr(db_connection, "get_db_path", lambda: path)
    monkeypatch.setattr(server_state, "xray_writer_guard", lambda **_kwargs: nullcontext())

    with db_connection.readonly_db_session_scope():
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            server_state.get_routing_global_state(expire_ttl=True)

    assert path.read_bytes() == before_bytes
    assert stat.S_IMODE(path.stat().st_mode) == before_db_mode
    assert stat.S_IMODE(tmp_path.stat().st_mode) == before_parent_mode
    assert path.stat().st_mtime_ns == before_db_mtime
    assert tmp_path.stat().st_mtime_ns == before_parent_mtime
    assert not Path(f"{path}-wal").exists()
    assert not Path(f"{path}-shm").exists()


def test_readonly_database_scope_resets_after_exception_and_is_context_local() -> None:
    from threading import Thread

    from fwrouter_api.db.connection import _READ_ONLY_DB_SESSIONS, readonly_db_session_scope

    observed_in_thread: list[bool] = []
    with pytest.raises(RuntimeError, match="fixture failure"):
        with readonly_db_session_scope():
            assert _READ_ONLY_DB_SESSIONS.get() is True
            thread = Thread(target=lambda: observed_in_thread.append(_READ_ONLY_DB_SESSIONS.get()))
            thread.start()
            thread.join(timeout=2)
            assert not thread.is_alive()
            raise RuntimeError("fixture failure")

    assert observed_in_thread == [False]
    assert _READ_ONLY_DB_SESSIONS.get() is False


def _proof_ready_db() -> dict[str, object]:
    recorded = _recorded_native_proof()
    return {
        "schema": {"ok": True, "expected_schema_version": 27, "actual_schema_version": 27},
        "integrity": ["ok"], "foreign_key_violations": 0, "inventory_digest": "internal",
        "inventory_count": 2,
        "generation": {"status": "verified", "candidate_digest": "internal", "verified_digest": "internal",
                        "candidate_revision": 4, "verified_revision": 4, "native_proof": recorded},
        "selection": {"revision": 8, "decision_id": "private", "active_server_id": "private"},
        "core_revision": 5, "source_fingerprint": "internal-secret-bearing-digest",
        "latest_apply": {"apply_id": "private-apply-id", "status": "applied", "core_intent_revision": 5},
        "routing": {"apply_state": "clean"},
        "modules": [
            {"module_name": name, "desired_state": "enabled", "lifecycle_mode": "managed", "apply_state": "clean"}
            for name in ("core", "vpn", "xray")
        ],
        "active_jobs": 0,
        "xray_identities": [],
}


def _recorded_native_proof(*, inventory_digest: str = "internal", generation_revision: int = 4) -> dict[str, object]:
    return {
        "schema": 1, "inventory_digest": inventory_digest, "generation_revision": generation_revision,
        "backend_manifest_sha256": "a" * 64,
        "xray": {key: value for key, value in {
            "config_sha256": "1" * 64, "runtime_config_sha256": "2" * 64,
            "runtime_incarnation_sha256": "3" * 64, "durable_bindings_sha256": "4" * 64,
            "durable_modes_sha256": "5" * 64, "runtime_image_digest": XRAY_IMAGE_PIN,
            "runtime_image_reference_sha256": "a" * 64, "runtime_version_sha256": "b" * 64,
        }.items()},
        "mihomo": {
            "config_sha256": "6" * 64, "semantic_config_sha256": "7" * 64,
            "durable_semantic_config_sha256": "8" * 64, "input_fingerprint_sha256": "9" * 64,
            "runtime_incarnation_sha256": "a" * 64, "runtime_receipt_config_sha256": "b" * 64,
            "runtime_receipt_fingerprint_sha256": "c" * 64, "runtime_image_digest": MIHOMO_IMAGE_PIN,
            "runtime_image_reference_sha256": "d" * 64, "runtime_version_sha256": "e" * 64,
        },
        "core": {
            "applied_manifest_sha256": "d" * 64, "applied_nft_sha256": "e" * 64,
            "native_rule_semantics_sha256": "f" * 64, "native_ip_semantics_sha256": "0" * 64,
            "latest_apply_id_sha256": __import__("hashlib").sha256(b"private-apply-id").hexdigest(),
            "core_intent_revision": 5, "selection_revision": 8,
        },
    }


def _patch_pass_dependencies(monkeypatch: pytest.MonkeyPatch, *, after_db: dict[str, object] | None = None) -> str:
    expected = "a" * 64
    monkeypatch.setattr(native_proof, "backend_manifest_sha256", lambda _root: expected)
    monkeypatch.setattr(native_proof, "_db_snapshot", lambda _path: _proof_ready_db())
    monkeypatch.setattr(native_proof, "_checkpoint_snapshot", lambda: {"present": False, "valid": True, "sha256": None})
    monkeypatch.setattr(native_proof, "_xray_readback", lambda _db: {
        "config_sha256": "1" * 64, "runtime_config_sha256": "2" * 64,
        "runtime_incarnation_sha256": "3" * 64, "durable_bindings_sha256": "4" * 64,
        "durable_modes_sha256": "5" * 64, "runtime_image_digest": XRAY_IMAGE_PIN,
        "runtime_image_reference_sha256": "a" * 64, "runtime_version_sha256": "b" * 64,
    })
    monkeypatch.setattr(native_proof, "_mihomo_readback", lambda _db: {
        "config_sha256": "6" * 64, "semantic_config_sha256": "7" * 64,
        "durable_semantic_config_sha256": "8" * 64, "input_fingerprint_sha256": "9" * 64,
        "runtime_incarnation_sha256": "a" * 64, "runtime_receipt_config_sha256": "b" * 64,
        "runtime_receipt_fingerprint_sha256": "c" * 64, "runtime_image_digest": MIHOMO_IMAGE_PIN,
        "runtime_image_reference_sha256": "d" * 64, "runtime_version_sha256": "e" * 64,
    })
    monkeypatch.setattr(native_proof, "_core_readback", lambda _db: {
        "applied_manifest_sha256": "d" * 64, "applied_nft_sha256": "e" * 64,
        "native_rule_semantics_sha256": "f" * 64,
        "native_ip_semantics_sha256": "0" * 64,
        "latest_apply_id_sha256": __import__("hashlib").sha256(b"private-apply-id").hexdigest(),
        "core_intent_revision": 5, "selection_revision": 8,
    })
    monkeypatch.setattr("fwrouter_api.db.connection.get_db_path", lambda: Path("/readonly/test.db"))

    from contextlib import contextmanager

    @contextmanager
    def read_guard():
        yield True

    monkeypatch.setattr("fwrouter_api.adapters.xray_common.xray_writer_read_guard", read_guard)
    if after_db is not None:
        calls = iter((_proof_ready_db(), after_db))
        monkeypatch.setattr(native_proof, "_db_snapshot", lambda _path: next(calls))
    return expected


def test_collect_native_proof_reports_pass_without_private_identifiers(monkeypatch: pytest.MonkeyPatch) -> None:
    expected = _patch_pass_dependencies(monkeypatch)

    result = native_proof.collect_native_proof(scope="ui", expected_backend_manifest_sha256=expected,
                                               expected_xray_image_digest=XRAY_IMAGE_PIN,
                                               expected_mihomo_image_digest=MIHOMO_IMAGE_PIN)

    assert result["status"] == "PASS", result
    assert result["proof_schema_version"] == 1
    assert "mihomo_config_rules_selectors_runtime" in result["checks_executed"]
    encoded = json.dumps(result, sort_keys=True)
    for private in ("private-apply-id", "internal-secret-bearing-digest", '"active_server_id"', '"decision_id"'):
        assert private not in encoded


def test_collect_native_proof_returns_stale_on_changed_durable_fence(monkeypatch: pytest.MonkeyPatch) -> None:
    changed = _proof_ready_db()
    changed["selection"] = {"revision": 9, "decision_id": "private", "active_server_id": "private"}
    expected = _patch_pass_dependencies(monkeypatch, after_db=changed)

    result = native_proof.collect_native_proof(scope="ui", expected_backend_manifest_sha256=expected,
                                               expected_xray_image_digest=XRAY_IMAGE_PIN,
                                               expected_mihomo_image_digest=MIHOMO_IMAGE_PIN)

    assert result["status"] == "STALE"
    assert result["reason"] == "durable_fence_changed"


def test_collect_native_proof_rejects_wrong_expected_manifest_before_native_reads(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(native_proof, "backend_manifest_sha256", lambda _root: "b" * 64)
    monkeypatch.setattr(native_proof, "_db_snapshot", lambda _path: pytest.fail("DB read must not run"))

    result = native_proof.collect_native_proof(scope="ui", expected_backend_manifest_sha256="a" * 64,
                                               expected_xray_image_digest=XRAY_IMAGE_PIN,
                                               expected_mihomo_image_digest=MIHOMO_IMAGE_PIN)

    assert result["status"] == "STALE"
    assert result["reason"] == "backend_manifest_changed"


def test_legacy_generation_without_native_tuple_is_incomplete(monkeypatch: pytest.MonkeyPatch) -> None:
    db = _proof_ready_db()
    db["generation"] = {key: value for key, value in db["generation"].items() if key != "native_proof"}
    expected = _patch_pass_dependencies(monkeypatch)
    monkeypatch.setattr(native_proof, "_db_snapshot", lambda _path: db)

    result = native_proof.collect_native_proof(scope="ui", expected_backend_manifest_sha256=expected,
                                               expected_xray_image_digest=XRAY_IMAGE_PIN,
                                               expected_mihomo_image_digest=MIHOMO_IMAGE_PIN)

    assert result["status"] == "INCOMPLETE"
    assert result["reason"] == "native_generation_receipt_missing_or_invalid"


def test_recorded_native_generation_tuple_mismatch_is_stale(monkeypatch: pytest.MonkeyPatch) -> None:
    db = _proof_ready_db()
    db["generation"]["native_proof"]["mihomo"]["semantic_config_sha256"] = "0" * 64
    expected = _patch_pass_dependencies(monkeypatch)
    monkeypatch.setattr(native_proof, "_db_snapshot", lambda _path: db)

    result = native_proof.collect_native_proof(scope="ui", expected_backend_manifest_sha256=expected,
                                               expected_xray_image_digest=XRAY_IMAGE_PIN,
                                               expected_mihomo_image_digest=MIHOMO_IMAGE_PIN)

    assert result["status"] == "STALE"
    assert result["reason"] == "native_generation_receipt_mismatch"


def test_runtime_change_between_component_reads_is_stale(monkeypatch: pytest.MonkeyPatch) -> None:
    expected = _patch_pass_dependencies(monkeypatch)
    original = native_proof._mihomo_readback
    calls = 0

    def changes_after_first_read(db):
        nonlocal calls
        calls += 1
        observed = original(db)
        if calls > 1:
            observed = dict(observed, runtime_incarnation_sha256="0" * 64)
        return observed

    monkeypatch.setattr(native_proof, "_mihomo_readback", changes_after_first_read)
    result = native_proof.collect_native_proof(
        scope="ui", expected_backend_manifest_sha256=expected,
        expected_xray_image_digest=XRAY_IMAGE_PIN, expected_mihomo_image_digest=MIHOMO_IMAGE_PIN,
    )
    assert result["status"] == "STALE"
    assert result["reason"] == "native_runtime_fence_changed"


def test_core_manifest_comparison_ignores_selector_owned_auto_target_but_checks_policy() -> None:
    core_manifest = {
        "desired_mode": "selective", "applied_mode": "selective", "selective_default": "direct",
        "server_mode": "auto", "desired_fixed_server_id": "", "applied_fixed_server_id": "",
        "active_auto_server_id": "stale-from-apply",
    }
    current_routing = {**core_manifest, "active_auto_server_id": "currently-selected"}

    assert native_proof._routing_manifest_matches(core_manifest, current_routing)
    assert not native_proof._routing_manifest_matches(core_manifest, {**current_routing, "desired_mode": "vpn"})


def test_mihomo_native_selector_must_match_persistent_auto_and_fixed_targets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from fwrouter_api.services import mihomo_config_status

    monkeypatch.setattr(mihomo_config_status, "_resolve_active_auto_runtime_target",
                        lambda _server_id, *, expected_targets: "server-a" if "server-a" in expected_targets else None)
    monkeypatch.setattr(mihomo_config_status, "_resolve_active_fixed_runtime_target",
                        lambda _server_id, *, expected_targets: "server-b" if "server-b" in expected_targets else None)
    config = {"proxies": [{"name": "server-a"}, {"name": "server-b"}]}

    assert native_proof._mihomo_selection_target_matches(
        {"routing": {"server_mode": "auto"}, "selection": {"active_server_id": "durable-a"}},
        config, {"vpn-auto": {"now": "server-a"}, "vpn-global": {"now": "vpn-auto"}},
    )
    assert not native_proof._mihomo_selection_target_matches(
        {"routing": {"server_mode": "auto"}, "selection": {"active_server_id": "durable-a"}},
        config, {"vpn-auto": {"now": "server-b"}, "vpn-global": {"now": "vpn-auto"}},
    )
    assert not native_proof._mihomo_selection_target_matches(
        {"routing": {"server_mode": "auto"}, "selection": {"active_server_id": "durable-a"}},
        config, {"vpn-auto": {"now": "server-a"}, "vpn-global": {"now": "REJECT"}},
    )
    assert native_proof._mihomo_selection_target_matches(
        {"routing": {"server_mode": "fixed", "applied_fixed_server_id": "durable-b"}},
        config, {"vpn-global": {"now": "server-b"}},
    )
    assert not native_proof._mihomo_selection_target_matches(
        {"routing": {"server_mode": "fixed", "applied_fixed_server_id": "durable-b"}},
        config, {"vpn-global": {"now": "server-a"}},
    )


def test_unknown_exception_text_is_never_returned_as_public_reason() -> None:
    assert native_proof._safe_failure_reason(RuntimeError("TOKEN_SECRET_123")) == "native_readback_unavailable"
    assert native_proof._safe_failure_reason(ValueError("CORE_NATIVE_RULES_MISMATCH")) == "CORE_NATIVE_RULES_MISMATCH"


def _nft_payload(*, extra_rule: bool = False, counter: int = 1, static_element: str = "198.51.100.1",
                 dynamic_element: str = "203.0.113.1", timeout: int = 30) -> dict[str, object]:
    table = {"family": "inet", "name": "fwrouter_v2", "handle": 1}
    chains = [
        {"family": "inet", "table": "fwrouter_v2", "name": "output", "type": "filter", "hook": "output",
         "prio": 0, "policy": "drop", "handle": 2},
    ]
    sets = [
        {"family": "inet", "table": "fwrouter_v2", "name": "static_allow", "type": "ipv4_addr", "handle": 3},
        {"family": "inet", "table": "fwrouter_v2", "name": "dns_direct_ipv4", "type": "ipv4_addr",
         "flags": ["timeout"], "timeout": timeout, "handle": 4},
    ]
    elements = [
        {"family": "inet", "table": "fwrouter_v2", "name": "static_allow", "elem": [static_element]},
        {"family": "inet", "table": "fwrouter_v2", "name": "dns_direct_ipv4", "elem": [dynamic_element]},
    ]
    rules = [{"family": "inet", "table": "fwrouter_v2", "chain": "output", "handle": 5,
              "expr": [{"counter": {"packets": counter, "bytes": counter * 10}}, {"accept": None}]}]
    if extra_rule:
        rules.append({"family": "inet", "table": "fwrouter_v2", "chain": "output", "handle": 6,
                      "expr": [{"accept": None}]})
    return {"nftables": [{"metainfo": {"version": "1.0"}}, {"table": table},
                         *[{"chain": row} for row in chains], *[{"set": row} for row in sets],
                         *[{"element": row} for row in elements], *[{"rule": row} for row in rules]]}


def test_core_native_semantic_digest_covers_rules_and_static_objects() -> None:
    baseline = native_proof.canonical_nft_table_sha256(_nft_payload())
    assert native_proof.canonical_nft_table_sha256(_nft_payload(counter=99, dynamic_element="192.0.2.5")) == baseline
    assert native_proof.canonical_nft_table_sha256(_nft_payload(extra_rule=True)) != baseline
    assert native_proof.canonical_nft_table_sha256(_nft_payload(static_element="198.51.100.2")) != baseline
    assert native_proof.canonical_nft_table_sha256(_nft_payload(timeout=31)) != baseline


def test_core_native_semantic_digest_preserves_rule_order_and_rejects_unknown_objects() -> None:
    payload = _nft_payload(extra_rule=True)
    rules = [entry["rule"] for entry in payload["nftables"] if "rule" in entry]
    payload["nftables"] = [entry for entry in payload["nftables"] if "rule" not in entry]
    payload["nftables"].extend({"rule": row} for row in reversed(rules))
    assert native_proof.canonical_nft_table_sha256(payload) != native_proof.canonical_nft_table_sha256(_nft_payload(extra_rule=True))
    with pytest.raises(ValueError, match="CORE_NFT_READBACK_UNSUPPORTED_OBJECT"):
        native_proof.canonical_nft_table({"nftables": [{"counter": {}}]})


def test_core_ip_receipt_hash_covers_priority_rules_and_full_table_routes(monkeypatch: pytest.MonkeyPatch) -> None:
    fixtures = {
        "-4": {
            "rules": [
                {"priority": 100, "fwmark": "0x100", "table": 100},
                {"priority": 101, "fwmark": "0x102", "table": 100},
            ],
            "routes": [{"type": "local", "dst": "default", "dev": "lo", "table": 100}],
        },
        "-6": {
            "rules": [
                {"priority": 100, "fwmark": "0x100", "table": 100},
                {"priority": 101, "fwmark": "0x102", "table": 100},
            ],
            "routes": [{"type": "local", "dst": "::/0", "dev": "lo", "table": 100}],
        },
    }

    def install(monkeypatch, facts):
        def run(command, **_kwargs):
            assert command[1] == "-N", "iproute2 table aliases must be disabled for semantic comparisons"
            family = command[3]
            payload = facts[family]["rules"] if "rule" in command else facts[family]["routes"]
            return subprocess.CompletedProcess(command, 0, stdout=json.dumps(payload), stderr="")
        monkeypatch.setattr(native_proof.subprocess, "run", run)

    install(monkeypatch, fixtures)
    baseline = native_proof._core_ip_facts()["semantic_sha256"]
    for local_type in ("local", "2"):
        local_route_facts = json.loads(json.dumps(fixtures))
        local_route_facts["-4"]["routes"][0]["type"] = local_type
        local_route_facts["-6"]["routes"][0]["type"] = local_type
        install(monkeypatch, local_route_facts)
        assert native_proof._core_ip_facts()["checks"]["-4_local_default_route"] is True
        assert native_proof._core_ip_facts()["checks"]["-6_local_default_route"] is True
    unicast_route_facts = json.loads(json.dumps(fixtures))
    unicast_route_facts["-4"]["routes"][0]["type"] = "1"
    unicast_route_facts["-6"]["routes"][0]["type"] = "unicast"
    install(monkeypatch, unicast_route_facts)
    with pytest.raises(ValueError, match="CORE_IP_POLICY_MISMATCH"):
        native_proof._core_ip_facts()

    install(monkeypatch, fixtures)
    default_spelling = json.loads(json.dumps(fixtures))
    default_spelling["-6"]["routes"][0]["dst"] = "default"
    install(monkeypatch, default_spelling)
    assert native_proof._core_ip_facts()["checks"]["-6_local_default_route"] is True
    assert native_proof._core_ip_facts()["semantic_sha256"] != baseline
    higher_priority = json.loads(json.dumps(fixtures))
    higher_priority["-4"]["rules"].insert(0, {"priority": 5, "to": "0.0.0.0/0", "table": "main"})
    install(monkeypatch, higher_priority)
    assert native_proof._core_ip_facts()["semantic_sha256"] != baseline
    changed_route = json.loads(json.dumps(fixtures))
    changed_route["-6"]["routes"][0]["dev"] = "eth0"
    install(monkeypatch, changed_route)
    with pytest.raises(ValueError, match="CORE_IP_POLICY_MISMATCH"):
        native_proof._core_ip_facts()


def test_optional_core_receipt_runtime_error_does_not_fail_existing_promotion(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    from fwrouter_api.services.dataplane_nft_artifacts import promote_last_good

    candidate = tmp_path / "candidate.nft"
    candidate.write_text("table inet fwrouter_v2 {}\n", encoding="utf-8")
    paths = {
        "candidate_nft_path": str(candidate),
        "current_nft_path": str(tmp_path / "current.nft"),
        "applied_nft_path": str(tmp_path / "applied.nft"),
        "last_good_nft_path": str(tmp_path / "last-good.nft"),
        "current_manifest_path": str(tmp_path / "current-manifest.json"),
        "applied_manifest_path": str(tmp_path / "applied-manifest.json"),
        "last_good_manifest_path": str(tmp_path / "last-good-manifest.json"),
    }

    def fail_with_runtime_error(**_kwargs: object) -> bool:
        raise RuntimeError("optional receipt failure")

    monkeypatch.setitem(
        sys.modules,
        "fwrouter_api.services.native_proof",
        SimpleNamespace(write_core_native_apply_receipt=fail_with_runtime_error),
    )
    promote_last_good(
        manifest={"plan_id": "fixture-apply", "routing_global_state": {"applied_mode": "direct"}},
        artifact_paths=paths,
        core_intent_revision=1,
    )

    assert Path(paths["applied_nft_path"]).read_text(encoding="utf-8") == candidate.read_text(encoding="utf-8")
    assert Path(paths["last_good_manifest_path"]).is_file()

def test_sqlite_generation_reader_keeps_legacy_marker_but_has_no_proof() -> None:
    from fwrouter_api.services.subscription import _read_subscription_native_generation

    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.executescript("""
        CREATE TABLE settings (key TEXT PRIMARY KEY, value_json TEXT NOT NULL);
        CREATE TABLE servers (server_id TEXT, server_name TEXT, provider_name TEXT, country_code TEXT,
                              region TEXT, raw_json TEXT, inventory_state TEXT);
        CREATE TABLE subscription_server_memberships (source_id TEXT, server_id TEXT,
                              entry_identity_hash TEXT, parser_format TEXT, is_active INTEGER);
    """)
    from fwrouter_api.services.subscription import _subscription_inventory_digest, SUBSCRIPTION_NATIVE_GENERATION_KEY

    digest, _ = _subscription_inventory_digest(connection)
    marker = {"schema": 1, "status": "verified", "candidate_digest": digest, "candidate_revision": 4,
              "verified_digest": digest, "verified_revision": 4}
    connection.execute("INSERT INTO settings VALUES (?, ?)", (SUBSCRIPTION_NATIVE_GENERATION_KEY, json.dumps(marker)))
    state = _read_subscription_native_generation(connection)
    assert state["status"] == "verified"
    assert state["native_proof"] is None


@pytest.mark.parametrize("capture_status", ["INCOMPLETE", "PASS"])
def test_generation_promotion_preserves_contract_and_persists_only_complete_proof(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capture_status: str,
) -> None:
    from fwrouter_api.services import subscription

    db_path = tmp_path / "generation.db"
    connection = sqlite3.connect(db_path)
    connection.row_factory = sqlite3.Row
    connection.executescript("""
        CREATE TABLE settings (key TEXT PRIMARY KEY, value_json TEXT NOT NULL, updated_at TEXT);
        CREATE TABLE servers (server_id TEXT, server_name TEXT, provider_name TEXT, country_code TEXT,
                              region TEXT, raw_json TEXT, inventory_state TEXT);
        CREATE TABLE subscription_server_memberships (source_id TEXT, server_id TEXT,
                              entry_identity_hash TEXT, parser_format TEXT, is_active INTEGER);
        CREATE TABLE apply_versions (apply_id TEXT, status TEXT, core_intent_revision INTEGER, created_at TEXT);
    """)
    digest, _ = subscription._subscription_inventory_digest(connection)
    connection.execute("INSERT INTO settings(key, value_json) VALUES (?, ?)", (subscription.SUBSCRIPTION_NATIVE_GENERATION_KEY,
        json.dumps({"schema": 1, "status": "pending", "candidate_digest": digest, "candidate_revision": 4,
                    "verified_digest": None, "verified_revision": None})))
    connection.execute("INSERT INTO apply_versions VALUES (?, 'applied', 5, '2026-10-23T00:00:00Z')",
                       ("private-apply-id",))
    connection.commit()

    @contextmanager
    def session():
        try:
            yield connection
            connection.commit()
        except BaseException:
            connection.rollback()
            raise

    monkeypatch.setattr(subscription, "db_session", session)
    monkeypatch.setattr(subscription, "xray_writer_guard_is_held", lambda: True)
    from fwrouter_api.services import mihomo_reconcile_fingerprint
    monkeypatch.setattr(mihomo_reconcile_fingerprint, "current_mihomo_input_fingerprint", lambda _routing: {"hash": "a" * 64})
    monkeypatch.setattr(mihomo_reconcile_fingerprint, "mihomo_udp_policy_receipt_matches", lambda: True)
    monkeypatch.setattr(mihomo_reconcile_fingerprint, "mihomo_input_unchanged", lambda _fingerprint: True)
    proof = _recorded_native_proof(inventory_digest=digest)
    monkeypatch.setattr(native_proof, "capture_native_generation_proof", lambda **_kw: (
        {"status": "PASS", "proof": proof} if capture_status == "PASS" else {"status": "INCOMPLETE"}
    ))
    monkeypatch.setattr("fwrouter_api.services.core_intent.read_core_intent_revision", lambda _connection: 5)
    monkeypatch.setattr("fwrouter_api.services.vpn_auto_selection_state.read_selection_fence",
                        lambda _connection: {"revision": 8})
    admission = subscription._SubscriptionInventoryAdmission(digest, 4, subscription._SUBSCRIPTION_ADMISSION_ISSUER)

    result = subscription._promote_subscription_inventory_generation(admission)

    marker = json.loads(connection.execute("SELECT value_json FROM settings WHERE key=?",
                                           (subscription.SUBSCRIPTION_NATIVE_GENERATION_KEY,)).fetchone()[0])
    assert result["ok"] is True
    assert marker["status"] == "verified"
    if capture_status == "PASS":
        assert marker["native_proof"] == proof
    else:
        assert "native_proof" not in marker
    connection.close()


def test_owner_capture_promotion_marker_and_readonly_collector_share_exact_proof(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    from fwrouter_api.services import subscription

    db_path = tmp_path / "generation-proof.db"
    connection = sqlite3.connect(db_path)
    connection.row_factory = sqlite3.Row
    connection.executescript("""
        CREATE TABLE settings (key TEXT PRIMARY KEY, value_json TEXT NOT NULL, updated_at TEXT);
        CREATE TABLE servers (server_id TEXT, server_name TEXT, provider_name TEXT, country_code TEXT,
                              region TEXT, raw_json TEXT, inventory_state TEXT);
        CREATE TABLE subscription_server_memberships (source_id TEXT, server_id TEXT,
                              entry_identity_hash TEXT, parser_format TEXT, is_active INTEGER);
        CREATE TABLE apply_versions (apply_id TEXT, status TEXT, core_intent_revision INTEGER, created_at TEXT);
    """)
    digest, count = subscription._subscription_inventory_digest(connection)
    connection.execute("INSERT INTO settings(key, value_json) VALUES (?, ?)", (subscription.SUBSCRIPTION_NATIVE_GENERATION_KEY,
        json.dumps({"schema": 1, "status": "pending", "candidate_digest": digest, "candidate_revision": 4,
                    "verified_digest": None, "verified_revision": None})))
    connection.execute("INSERT INTO apply_versions VALUES (?, 'applied', 5, '2026-10-23T00:00:00Z')",
                       ("private-apply-id",))
    connection.commit()

    @contextmanager
    def session():
        try:
            yield connection
            connection.commit()
        except BaseException:
            connection.rollback()
            raise

    monkeypatch.setattr(subscription, "db_session", session)
    monkeypatch.setattr(subscription, "xray_writer_guard_is_held", lambda: True)
    from fwrouter_api.services import mihomo_reconcile_fingerprint
    monkeypatch.setattr(mihomo_reconcile_fingerprint, "current_mihomo_input_fingerprint", lambda _routing: {"hash": "a" * 64})
    monkeypatch.setattr(mihomo_reconcile_fingerprint, "mihomo_udp_policy_receipt_matches", lambda: True)
    monkeypatch.setattr(mihomo_reconcile_fingerprint, "mihomo_input_unchanged", lambda _fingerprint: True)
    monkeypatch.setattr("fwrouter_api.services.core_intent.read_core_intent_revision", lambda _connection: 5)
    monkeypatch.setattr("fwrouter_api.services.vpn_auto_selection_state.read_selection_fence",
                        lambda _connection: {"revision": 8})

    recorded = _recorded_native_proof(inventory_digest=digest)
    xray_evidence = dict(recorded["xray"], config_matches_mounted=True, runtime_incarnation_stable=True,
                         identity_count=0, bindings_match=True, modes_match=True)
    mihomo_evidence = dict(recorded["mihomo"], config_stable=True, runtime_incarnation_stable=True,
                           selector_count=0, native_rule_count=0, native_top_level_rules_match=True,
                           selector_membership_matches=True, guarded_rules_match=True,
                           selection_target_matches_durable_state=True)
    core_evidence = dict(recorded["core"], apply_owned_by_latest_revision=True, applied_manifest_stable=True,
                         applied_nft_stable=True, native_rule_semantics_match_owner_receipt=True)
    owner_snapshot = {
        "schema": {"ok": True}, "integrity": ["ok"], "foreign_key_violations": 0,
        "inventory_digest": digest, "inventory_count": count,
        "generation": {"status": "pending", "inventory_digest": digest,
                       "candidate_digest": digest, "candidate_revision": 4},
    }
    monkeypatch.setattr(native_proof, "backend_manifest_sha256", lambda _root: "a" * 64)
    monkeypatch.setattr(native_proof, "_db_snapshot", lambda _path: dict(owner_snapshot))
    monkeypatch.setattr(native_proof, "_checkpoint_snapshot", lambda: {"present": False, "valid": True, "sha256": None})
    from fwrouter_api.db import connection as db_connection

    def readonly_reader(evidence):
        def read(_db):
            assert db_connection._READ_ONLY_DB_SESSIONS.get() is True
            return dict(evidence)
        return read

    monkeypatch.setattr(native_proof, "_xray_readback", readonly_reader(xray_evidence))
    monkeypatch.setattr(native_proof, "_mihomo_readback", readonly_reader(mihomo_evidence))
    monkeypatch.setattr(native_proof, "_core_readback", readonly_reader(core_evidence))
    monkeypatch.setattr("fwrouter_api.db.connection.get_db_path", lambda: db_path)
    from fwrouter_api.adapters import xray_common
    monkeypatch.setattr(xray_common, "xray_writer_guard_is_held", lambda: True)
    admission = subscription._SubscriptionInventoryAdmission(digest, 4, subscription._SUBSCRIPTION_ADMISSION_ISSUER)

    promoted = subscription._promote_subscription_inventory_generation(admission)
    assert db_connection._READ_ONLY_DB_SESSIONS.get() is False
    marker = json.loads(connection.execute("SELECT value_json FROM settings WHERE key=?",
                                           (subscription.SUBSCRIPTION_NATIVE_GENERATION_KEY,)).fetchone()[0])
    assert promoted["ok"] is True
    assert marker["status"] == "verified"
    assert marker["native_proof"]["inventory_digest"] == digest
    assert marker["native_proof"]["generation_revision"] == 4

    verified_db = _proof_ready_db()
    verified_db["inventory_digest"] = digest
    verified_db["inventory_count"] = count
    verified_db["generation"].update({
        "candidate_digest": digest, "verified_digest": digest,
        "native_proof": marker["native_proof"],
    })
    monkeypatch.setattr(native_proof, "_db_snapshot", lambda _path: dict(verified_db))
    expected = "a" * 64
    from contextlib import contextmanager as contextmanager_factory

    @contextmanager_factory
    def read_guard():
        yield True

    monkeypatch.setattr(xray_common, "xray_writer_read_guard", read_guard)
    monkeypatch.setattr(native_proof, "_xray_readback", readonly_reader(xray_evidence))
    monkeypatch.setattr(native_proof, "_mihomo_readback", readonly_reader(mihomo_evidence))
    monkeypatch.setattr(native_proof, "_core_readback", readonly_reader(core_evidence))
    result = native_proof.collect_native_proof(
        scope="ui", expected_backend_manifest_sha256=expected,
        expected_xray_image_digest=XRAY_IMAGE_PIN, expected_mihomo_image_digest=MIHOMO_IMAGE_PIN,
    )
    assert result["status"] == "PASS", result
    assert db_connection._READ_ONLY_DB_SESSIONS.get() is False
    connection.close()


def test_xray_read_guard_fails_closed_without_creating_lock_or_directory(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from fwrouter_api.adapters import xray_common

    run_dir = tmp_path / "missing"
    monkeypatch.setattr(xray_common, "get_settings", lambda: SimpleNamespace(paths=SimpleNamespace(run_dir=run_dir)))

    with xray_common.xray_writer_read_guard() as acquired:
        assert acquired is False

    assert not run_dir.exists()


def test_xray_read_guard_is_nonblocking_for_writer_and_rejects_same_thread(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from fwrouter_api.adapters import xray_common

    run_dir = tmp_path / "run"
    run_dir.mkdir()
    lock_path = run_dir / "xray-writer.lock"
    lock_path.touch()
    monkeypatch.setattr(xray_common, "get_settings", lambda: SimpleNamespace(paths=SimpleNamespace(run_dir=run_dir)))

    writer_fd = os.open(lock_path, os.O_RDWR)
    import fcntl
    fcntl.flock(writer_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        with xray_common.xray_writer_read_guard() as acquired:
            assert acquired is False
    finally:
        fcntl.flock(writer_fd, fcntl.LOCK_UN)
        os.close(writer_fd)

    with xray_common.xray_writer_guard():
        with xray_common.xray_writer_read_guard() as acquired:
            assert acquired is False


def test_xray_read_guard_rejects_lock_inode_replacement_and_permission_drift(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    from fwrouter_api.adapters import xray_common

    run_dir = tmp_path / "run"
    run_dir.mkdir()
    lock_path = run_dir / "xray-writer.lock"
    lock_path.touch(mode=0o600)
    lock_path.chmod(0o600)
    monkeypatch.setattr(xray_common, "get_settings", lambda: SimpleNamespace(paths=SimpleNamespace(run_dir=run_dir)))

    with pytest.raises(RuntimeError, match="XRAY_READ_LOCK_CHANGED"):
        with xray_common.xray_writer_read_guard() as acquired:
            assert acquired is True
            lock_path.unlink()
            lock_path.touch(mode=0o600)
            lock_path.chmod(0o600)

    with pytest.raises(RuntimeError, match="XRAY_READ_LOCK_CHANGED"):
        with xray_common.xray_writer_read_guard() as acquired:
            assert acquired is True
            lock_path.chmod(0o644)


def test_default_xray_read_commands_do_not_create_docker_cli_state(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from fwrouter_api.adapters.xray_real import RealXrayAdapter

    state_dir = tmp_path / "docker-state"
    monkeypatch.setattr("fwrouter_api.adapters.xray_real.DOCKER_CLI_STATE_DIR", state_dir)
    seen: dict[str, object] = {}

    def run(command, **kwargs):
        seen["env"] = kwargs["env"]
        return subprocess.CompletedProcess(command, 0, stdout="container-id\n", stderr="")

    monkeypatch.setattr("fwrouter_api.adapters.xray_real.subprocess.run", run)
    adapter = RealXrayAdapter()
    response = adapter._default_runner("runtime_container_id", {})

    assert response.ok
    assert not state_dir.exists()
    assert seen["env"]["HOME"] == "/proc"
    assert "DOCKER_CONFIG" not in seen["env"]


def test_mihomo_runtime_identity_checks_container_image_pin_and_version(monkeypatch: pytest.MonkeyPatch) -> None:
    from fwrouter_api.services import subscription_pipeline

    container_id = "a" * 64
    image_digest = "sha256:" + "b" * 64
    image_reference = subscription_pipeline.MIHOMO_IMAGE
    calls: list[list[str]] = []

    def docker_readonly(arguments: list[str], timeout: float = 4.0) -> str:
        del timeout
        calls.append(arguments)
        if arguments[0:2] == ["compose", "-f"] and "ps" in arguments:
            return container_id
        if arguments[0] == "inspect":
            return f"{container_id}|{image_digest}|{image_reference}|true|2026-10-23T00:00:00Z"
        if arguments[0:2] == ["compose", "-f"] and arguments[-2:] == ["config", "--images"]:
            return image_reference
        raise AssertionError(f"unexpected read-only Docker command shape: {arguments[:2]}")

    class Adapter:
        def __init__(self, version: str) -> None:
            self.version = version

        def _get_json(self, path: str, *, timeout_seconds: float) -> dict[str, str]:
            assert path == "/version"
            assert timeout_seconds == 2.0
            return {"version": self.version}

    monkeypatch.setattr(native_proof, "_docker_readonly", docker_readonly)
    observed = native_proof._mihomo_runtime_identity(Adapter("v1.19.31"))
    assert observed["image_digest"] == image_digest
    assert observed["image_reference_sha256"] == __import__("hashlib").sha256(image_reference.encode()).hexdigest()
    assert observed["version_sha256"] == __import__("hashlib").sha256(b"v1.19.31").hexdigest()
    assert len(calls) == 3

    with pytest.raises(ValueError, match="MIHOMO_RUNTIME_VERSION_MISMATCH"):
        native_proof._mihomo_runtime_identity(Adapter("v1.19.32"))

    def wrong_image(arguments: list[str], timeout: float = 4.0) -> str:
        if arguments[0] == "inspect":
            return f"{container_id}|{image_digest}|untrusted:mutable|true|2026-10-23T00:00:00Z"
        return docker_readonly(arguments, timeout)

    monkeypatch.setattr(native_proof, "_docker_readonly", wrong_image)
    with pytest.raises(ValueError, match="MIHOMO_RUNTIME_IMAGE_REFERENCE_MISMATCH"):
        native_proof._mihomo_runtime_identity(Adapter("v1.19.31"))
