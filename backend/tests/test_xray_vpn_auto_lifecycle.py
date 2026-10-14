from __future__ import annotations

import json
import hashlib
from typing import Any
import pytest

from _test_support import database_table_snapshot as _database_snapshot
from fwrouter_api.db.connection import db_session
from fwrouter_api.services import subject_policy as subject_policy_service
from fwrouter_api.services.subscription_profiles import list_desired_subscription_xray_clients, promote_runtime_verified_subscription_nodes
from pathlib import Path

from _test_support import configure_test_state_dir
from fwrouter_api.adapters.xray import RealXrayAdapter
from fwrouter_api.db.connection import initialize_database
from fwrouter_api.services import xray_subscription_service
from test_xray import (
    _configure_env,
    _run_profile_candidate_validation_case,
    _seed_server,
    _seed_routing_state,
    _seed_subscription_identity,
    _FakeRunner,
    _build_adapter,
    _enable_xray_module,
    _patch_runtime,
    _patch_xray_adapters,
    _write_xray_config,
    _xray_paths,
)


class _PythonComponentXrayAdapter:
    """Use the real config adapter while keeping native generation checks separate."""

    def __init__(self, component: RealXrayAdapter) -> None:
        self._component = component

    def __getattr__(self, name: str):
        if name == "stage_subscription_generation":
            raise AttributeError(name)
        return getattr(self._component, name)


def _install_runtime_boundaries(monkeypatch, adapter: _PythonComponentXrayAdapter) -> None:
    monkeypatch.setattr(xray_subscription_service, "_xray_adapter", lambda: adapter)
    monkeypatch.setattr(
        xray_subscription_service,
        "_materialize_xray_runtime_bindings",
        lambda **_kwargs: {"ok": True, "status": "applied"},
    )
    monkeypatch.setattr(
        "fwrouter_api.services.mihomo_config.reconcile_mihomo_runtime",
        lambda **_kwargs: {"ok": True, "reconcile_action": "none"},
    )


def test_vpn_auto_reconcile_removes_stale_identity_and_recreates_one_stable_identity(
    monkeypatch, tmp_path: Path,
) -> None:
    configure_test_state_dir(monkeypatch, tmp_path)
    initialize_database()
    _patch_runtime(monkeypatch)
    _enable_xray_module()

    stale_email = xray_subscription_service._vpn_auto_xray_client_email("server-gone")
    config_path, _ = _xray_paths()
    _write_xray_config(config_path, [
        {"id": "stale-uuid", "email": stale_email},
        {"id": "manual-uuid", "email": "manual@example.test"},
    ])
    runner = _FakeRunner()
    component = _build_adapter(tmp_path, runner=runner)
    _patch_xray_adapters(monkeypatch, component)
    adapter = _PythonComponentXrayAdapter(component)
    _install_runtime_boundaries(monkeypatch, adapter)

    removed = xray_subscription_service.reconcile_xray_vpn_auto_subscription(requested_by="pytest")

    assert removed["ok"] is True
    assert removed["deleted_count"] == 1
    clients = json.loads(config_path.read_text(encoding="utf-8"))["inbounds"][0]["settings"]["clients"]
    assert stale_email not in {client["email"] for client in clients}
    assert "manual@example.test" in {client["email"] for client in clients}
    virtual_email = xray_subscription_service._vpn_auto_xray_client_email(
        xray_subscription_service.VIRTUAL_XRAY_VPN_AUTO_SERVER_ID
    )
    first_virtual = next(client for client in clients if client["email"] == virtual_email)

    first_return = xray_subscription_service.reconcile_xray_vpn_auto_subscription(requested_by="pytest")
    second_clients = json.loads(config_path.read_text(encoding="utf-8"))["inbounds"][0]["settings"]["clients"]
    second_virtual = next(client for client in second_clients if client["email"] == virtual_email)

    assert first_return["ok"] is True
    assert first_return["created_count"] == 0
    assert first_return["deleted_count"] == 0
    assert second_virtual["id"] == first_virtual["id"]
    assert len([client for client in second_clients if client["email"] == virtual_email]) == 1


@pytest.mark.parametrize("recorded_digest", [True, False], ids=["changed-active-config", "missing-transition-proof"])
def test_transition_recovery_requires_recorded_config_digest_before_native_mutation(
    monkeypatch, tmp_path: Path, recorded_digest: bool,
) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()
    _patch_runtime(monkeypatch)
    _enable_xray_module()
    adapter = _build_adapter(tmp_path, runner=_FakeRunner())
    _patch_xray_adapters(monkeypatch, adapter)
    xray_path, _ = _xray_paths()
    _write_xray_config(xray_path, [])
    from fwrouter_api.services import mihomo_config, mihomo_runtime
    mihomo_path = Path(mihomo_config._resolved_base_config_path())
    mihomo_path.parent.mkdir(parents=True, exist_ok=True)
    mihomo_path.write_text("owned-transition", encoding="utf-8")
    transition_hash = hashlib.sha256(mihomo_path.read_bytes()).hexdigest()
    validation = {"xray": {"expected_client_identities": []}}
    if recorded_digest:
        validation["transition"] = {"candidate_sha256": transition_hash}
    monkeypatch.setattr(mihomo_runtime, "get_mihomo_runtime_incarnation", lambda: "owned-incarnation")
    checkpoint = xray_subscription_service._write_xray_generation_checkpoint(
        adapter=adapter, generation_id="transition-proof-fixture", tokens=set(), phase="xray_applied",
        source_fingerprint=xray_subscription_service._generation_source_fingerprint(),
        staged_generation={"native_validation": validation}, managed_email_prefixes=["sub-"],
    )
    stored = json.loads(checkpoint.read_text())
    assert stored["staged_generation"]["mihomo_transition_sha256"] == (transition_hash if recorded_digest else None)
    if recorded_digest:
        mihomo_path.write_text("foreign-config-same-incarnation", encoding="utf-8")
    before = (xray_path.read_bytes(), mihomo_path.read_bytes(), checkpoint.read_bytes())
    revisions_before = _database_snapshot()
    def unexpected_native_read_or_write(*_args, **_kwargs):
        raise AssertionError("unproven transition must be refused before native mutation")
    monkeypatch.setattr(adapter, "reload", unexpected_native_read_or_write)
    monkeypatch.setattr(adapter, "test_config", unexpected_native_read_or_write)
    result = xray_subscription_service._restore_xray_generation_checkpoint(adapter, checkpoint)
    assert result == {"ok": False, "recovered": "stale_checkpoint_declined", "reason": (
        "active_mihomo_generation_changed" if recorded_digest else "mihomo_transition_digest_unavailable"
    )}
    assert (xray_path.read_bytes(), mihomo_path.read_bytes(), checkpoint.read_bytes()) == before
    assert _database_snapshot() == revisions_before


def test_inventory_replacement_changes_only_its_derived_profile_identity_before_publication(monkeypatch, tmp_path: Path) -> None:
    """Inventory identity convergence must not silently publish unverified nodes."""
    _configure_env(monkeypatch, tmp_path)
    initialize_database()
    tokens = [f"inventory-delta-{index}" for index in range(7)]
    for token in tokens:
        _seed_subscription_identity(slug=token, token=token)
    from fwrouter_api.adapters.subscription import parse_subscription_payload

    def member(index):
        return {
            "tag": f"owned-member-{index}", "protocol": "vless",
            "settings": {"vnext": [{"address": f"member-{index}.example.invalid", "port": 443,
                                      "users": [{"id": f"owned-fixture-id-{index}"}]}]},
            "streamSettings": {"network": "tcp", "security": "tls",
                               "tlsSettings": {"serverName": f"member-{index}.example.invalid"}},
        }

    service_outbounds = [{"tag": "direct", "protocol": "freedom"},
                         {"tag": "reject", "protocol": "blackhole"},
                         {"tag": "dns", "protocol": "dns"}]
    original_profile = {"remarks": "owned-growth-profile",
                        "outbounds": [*(member(index) for index in range(7)), *service_outbounds]}
    changed_profile = json.loads(json.dumps(original_profile))
    changed_profile["outbounds"] = [*changed_profile["outbounds"][:7],
                                    *(member(index) for index in range(7, 10)), *service_outbounds]
    old_result = parse_subscription_payload(json.dumps(original_profile))
    new_result = parse_subscription_payload(json.dumps(changed_profile))
    assert old_result.ok and new_result.ok
    assert len(old_result.servers) == len(new_result.servers) == 1
    retiring = old_result.servers[0]
    replacement = new_result.servers[0]
    assert retiring.server_name == replacement.server_name
    assert retiring.host == replacement.host
    assert retiring.server_id != replacement.server_id
    assert len(retiring.raw["_fwrouter_topology"]["endpoints"]) == 7
    assert len(replacement.raw["_fwrouter_topology"]["endpoints"]) == 10
    assert original_profile["outbounds"][:7] == changed_profile["outbounds"][:7]
    for profile, server in ((original_profile, retiring), (changed_profile, replacement)):
        canonical = json.dumps(profile, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        assert server.server_id == "sub:" + hashlib.sha256(canonical.encode()).hexdigest()
    _seed_server("server-retained")
    _seed_server(retiring.server_id, server_name=retiring.server_name, raw=retiring.raw)
    original = {token: list_desired_subscription_xray_clients(token) for token in tokens}
    for nodes in original.values():
        promote_runtime_verified_subscription_nodes(nodes)
    from uuid import NAMESPACE_DNS, uuid5
    snapshots_before = _database_snapshot()["subscription_profile_snapshots"]
    _seed_server(replacement.server_id, server_name=replacement.server_name, raw=replacement.raw)
    with db_session() as connection:
        connection.execute("UPDATE servers SET inventory_state = 'missing' WHERE server_id = ?", (retiring.server_id,))
    for token in tokens:
        old = {node["server_id"]: node for node in original[token]}
        new = {node["server_id"]: node for node in list_desired_subscription_xray_clients(token)}
        assert retiring.server_id not in new and replacement.server_id in new
        assert new["server-retained"]["client_uuid"] == old["server-retained"]["client_uuid"]
        assert new["server-retained"]["client_email"] == old["server-retained"]["client_email"]
        assert new[replacement.server_id]["client_uuid"] == str(uuid5(NAMESPACE_DNS, f"fwrouter-subscription:{token}:{replacement.server_id}"))
        assert new[replacement.server_id]["client_uuid"] != old[retiring.server_id]["client_uuid"]
        published = next(row for row in snapshots_before if row["token"] == token)
        published_ids = {node["client_uuid"] for node in json.loads(published["nodes_json"])}
        assert old[retiring.server_id]["client_uuid"] in published_ids
        assert new[replacement.server_id]["client_uuid"] not in published_ids
    assert _database_snapshot()["subscription_profile_snapshots"] == snapshots_before


@pytest.mark.parametrize("client_count", [1, 70])
def test_profile_binding_plan_reuses_owned_enforcement_without_per_client_native_preflight(monkeypatch, tmp_path: Path, client_count: int) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()
    _patch_runtime(monkeypatch)
    _seed_server("profile-plan-server")
    _seed_routing_state(desired_mode="vpn")
    # This component has no native DNS/network namespace. Keep DNS explicitly
    # unavailable rather than discovering the runner host or faking proof.
    monkeypatch.setattr(
        "fwrouter_api.services.dnsmasq.inspect_dnsmasq_selective_status",
        lambda: {"ok": False, "reason": "component_dns_namespace_unavailable"},
    )
    nodes = []
    with db_session() as connection:
        for index in range(client_count):
            client_id = f"owned-profile-client-{index}"
            email = f"sub-owned-plan-{index}@example.test"
            subject_id = f"xray:{client_id}"
            connection.execute(
                """INSERT INTO subjects (subject_id, subject_type, subject_role, implementation_kind,
                    stable_key, display_name, desired_mode, runtime_state, is_active, is_deleted, metadata_json)
                    VALUES (?, 'explicit_external_client', 'vless_client', 'xray', ?, ?, 'enabled', 'active', 1, 0, ?)""",
                (subject_id, subject_id, email, json.dumps({"detail": {
                    "client_id": client_id, "client_uuid": client_id, "email": email, "enabled": True,
                }})),
            )
            nodes.append({"client_uuid": client_id, "client_email": email, "server_id": "profile-plan-server", "xray_alias": "owned-profile"})
    from fwrouter_api.services import xray_client_state
    def unexpected_per_client_enrichment(*_args, **_kwargs):
        raise AssertionError("planner must enrich persisted identity with its owned snapshot only")
    monkeypatch.setattr(xray_client_state, "get_subject_with_effective_state", unexpected_per_client_enrichment)
    preflight_calls = []
    original = subject_policy_service._default_subject_runtime_enforcement
    def observed_preflight(**kwargs):
        preflight_calls.append(True)
        return original(**kwargs)
    monkeypatch.setattr(subject_policy_service, "_default_subject_runtime_enforcement", observed_preflight)
    before = _database_snapshot()
    bindings, modes, assignments = xray_subscription_service._prospective_profile_bindings(
        nodes, token_prefix="sub-", preserve_existing_overrides=False,
    )
    assert len(preflight_calls) == 1  # Existing mode projection takes one shared preflight.
    assert len(bindings) == client_count and modes == []
    assert {row["client_uuid"] for row in bindings} == {row["client_uuid"] for row in nodes}
    assert all(row["selected_server_id"] == "profile-plan-server" for row in bindings)
    assert len(assignments) == 1
    assert _database_snapshot() == before
    # Ordinary callers retain the original fresh effective-state path.
    with pytest.raises(AssertionError, match="planner must enrich"):
        xray_client_state._xray_subject_for_client(nodes[0]["client_uuid"])


@pytest.mark.parametrize("case", ["identical", "different-handoffs", "changed-inputs", "copy-corrupted"])
def test_identical_candidate_copy_retains_both_validations_and_rejects_changed_context(monkeypatch, tmp_path: Path, case: str) -> None:
    old = [{"selected_server_id": "retiring-target", "port": 53124}] if case == "different-handoffs" else []
    if case == "copy-corrupted":
        from fwrouter_api.services import artifacts
        original_copy = artifacts.atomic_copy_file
        def corrupt_copy(source, target):
            original_copy(source, target)
            Path(target).write_text("foreign final candidate", encoding="utf-8")
        monkeypatch.setattr(artifacts, "atomic_copy_file", corrupt_copy)
    result, local_calls, native_calls = _run_profile_candidate_validation_case(
        monkeypatch, tmp_path, old_assignments=old,
        assignments=[{"selected_server_id": "owned-target", "port": 53123}],
        image_id=None, input_context_changes=case == "changed-inputs",
    )
    if case == "copy-corrupted":
        assert result["ok"] is False
        assert result["error_code"] == "MIHOMO_STAGE_CANDIDATE_COPY_MISMATCH"
        assert len(local_calls) == 1 and len(native_calls) == 1
        return
    assert result["ok"] is True
    assert len(local_calls) == 2 and len(native_calls) == 2
    transition = result["native_validation"]["transition"]
    final = result["native_validation"]["final"]
    assert transition["validation"]["candidate_write_reused"] is False
    assert final["validation"]["candidate_write_reused"] is (case == "identical")
    assert final["validation"]["native_validation_reused"] is False
    if case != "different-handoffs":
        assert transition["candidate_sha256"] == final["candidate_sha256"]
    else:
        assert transition["candidate_sha256"] != final["candidate_sha256"]
