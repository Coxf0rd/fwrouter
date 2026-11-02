from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

from fwrouter_api.db.connection import db_session
from fwrouter_api.services import xray_subscription_service as service
from fwrouter_api.services import xray_profile_planning as profile_planning


def _seed_server(
    server_id: str,
    *,
    name: str | None = None,
    raw_json: str | None = None,
    vpn_auto: bool = False,
    ping_status: str | None = None,
    ping_ms: int | None = None,
) -> None:
    with db_session() as connection:
        connection.execute(
            "INSERT INTO servers (server_id, server_name, raw_json, inventory_state) VALUES (?, ?, ?, 'active')",
            (server_id, name or server_id, raw_json or "{}"),
        )
        connection.execute(
            "INSERT INTO server_preferences (server_id, vpn_auto, vpn_auto_priority) VALUES (?, ?, 0)",
            (server_id, int(vpn_auto)),
        )
        if ping_status is not None:
            connection.execute(
                "INSERT INTO server_ping_state (server_id, status, last_ping_ms) VALUES (?, ?, ?)",
                (server_id, ping_status, ping_ms),
            )


def _supported_vless(address: str = "node.example.test") -> str:
    return json.dumps({
        "type": "vless",
        "network": "tcp",
        "tls": True,
        "reality-opts": {"public-key": "public-key"},
        "uuid": "11111111-1111-4111-8111-111111111111",
        "server": address,
        "port": 443,
    })


def _seed_subject(subject_id: str, metadata: dict, *, active: int = 1, deleted: int = 0) -> None:
    with db_session() as connection:
        connection.execute(
            """INSERT INTO subjects (
                   subject_id, subject_type, subject_role, implementation_kind, stable_key,
                   display_name, desired_mode, is_active, is_deleted, metadata_json
               ) VALUES (?, 'explicit_external_client', 'vless_client', 'xray', ?, ?, 'vpn', ?, ?, ?)""",
            (subject_id, subject_id, subject_id, active, deleted, json.dumps(metadata)),
        )


def test_vpn_auto_projection_keeps_virtual_proxy_and_sql_candidate_order() -> None:
    _seed_server("normal-slower", raw_json=_supported_vless(), vpn_auto=True,
                 ping_status="success", ping_ms=24)
    _seed_server("normal-faster", raw_json=_supported_vless("fast.example.test"), vpn_auto=True,
                 ping_status="success", ping_ms=8)
    _seed_server("normal-failed", raw_json=_supported_vless(), vpn_auto=True,
                 ping_status="failed", ping_ms=1)
    _seed_server("normal-unsupported", raw_json=json.dumps({"type": "vmess"}), vpn_auto=True,
                 ping_status="success", ping_ms=2)
    _seed_server("proxy", name="Proxy host", vpn_auto=True,
                 ping_status="success", ping_ms=0)
    with db_session() as connection:
        connection.execute(
            "INSERT INTO server_custom_https_proxy (server_id, host, port) VALUES ('proxy', 'proxy.example.test', 443)"
        )

    result = service._vpn_auto_servers_for_xray_subscription()

    assert [item["server_id"] for item in result] == [
        "virtual:xray:vpn-auto", "proxy", "normal-faster", "normal-slower", "normal-failed",
    ]
    assert result[0]["support_reason"] == "virtual_xray_vpn_auto"
    assert result[1]["support_reason"] == "custom_https_proxy"
    assert all(item["support_reason"] == "supported" for item in result[2:])


def test_vpn_auto_sql_preserves_current_malformed_json_failure() -> None:
    _seed_server("normal-malformed", raw_json="{broken", vpn_auto=True)

    with pytest.raises(sqlite3.OperationalError, match="malformed JSON"):
        service._vpn_auto_servers_for_xray_subscription()


def test_generation_input_compiler_preserves_order_last_email_value_and_prefix_rules() -> None:
    nodes = [
        {"client_uuid": "id-first", "client_email": "same@example.test", "xray_alias": "First"},
        {"client_uuid": "id-second", "client_email": "same@example.test", "xray_alias": "Second"},
        {"client_uuid": "id-no-email", "client_email": "", "xray_alias": "No email"},
    ]

    compiled = profile_planning.compile_generation_inputs(
        nodes, token_or_slug=" Team A ", include_vpn_auto=True,
    )

    assert list(compiled.desired_by_email) == ["same@example.test"]
    assert compiled.desired_by_email["same@example.test"] is nodes[1]
    assert compiled.desired_clients == [
        {"client_uuid": "id-first", "client_id": "id-first", "email": "same@example.test", "alias": "First"},
        {"client_uuid": "id-second", "client_id": "id-second", "email": "same@example.test", "alias": "Second"},
        {"client_uuid": "id-no-email", "client_id": "id-no-email", "email": "", "alias": "No email"},
    ]
    assert compiled.token_prefix == f"sub-{profile_planning._stable_digest('team a', length=10)}-"
    assert compiled.managed_email_prefixes == [compiled.token_prefix]
    with pytest.raises(FrozenInstanceError):
        compiled.token_prefix = "changed"  # type: ignore[misc]

    auto = profile_planning.compile_generation_inputs(
        [nodes[0]], token_or_slug=None, include_vpn_auto=True,
    )
    ordinary = profile_planning.compile_generation_inputs(
        [nodes[0]], token_or_slug=None, include_vpn_auto=False,
    )
    assert auto.managed_email_prefixes == ["sub-", "vpn-auto-"]
    assert ordinary.managed_email_prefixes == ["sub-"]


def test_generation_input_compiler_preserves_direct_key_failure() -> None:
    with pytest.raises(KeyError, match="xray_alias"):
        profile_planning.compile_generation_inputs(
            [{"client_uuid": "id", "client_email": "id@example.test"}],
            token_or_slug=None,
            include_vpn_auto=False,
        )


def test_prospective_binding_projection_preserves_read_order_fallback_and_selective_mode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _seed_server("requested-server")
    calls: list[str] = []
    monkeypatch.setattr(service, "collect_xray_runtime_bindings", lambda **_kwargs: (
        calls.append("bindings") or [
            {"subject_id": "unmanaged", "client_email": "other@example.test"},
            {"subject_id": "managed-old", "client_email": "sub-old@example.test"},
        ]
    ))
    monkeypatch.setattr(service, "collect_xray_client_mode_directives", lambda **_kwargs: (
        calls.append("modes") or [{"subject_id": "old-mode", "client_email": "other@example.test"}]
    ))
    monkeypatch.setattr(service.subject_policy_service, "get_routing_snapshot", lambda: calls.append("routing") or {"revision": 4})
    monkeypatch.setattr(service.subject_policy_service, "build_runtime_enforcement_state", lambda: calls.append("enforcement") or {"ready": True})
    monkeypatch.setattr(service.subject_policy_service, "get_core_bypass_state", lambda: calls.append("bypass") or {"enabled": False})
    monkeypatch.setattr(service, "_xray_subject_for_client", lambda client_id, **_kwargs: (
        calls.append(f"subject:{client_id}") or (
            {"subject_id": f"persisted:{client_id}", "detail": {"client_uuid": client_id}}
            if client_id == "known-id" else None
        )
    ))
    monkeypatch.setattr(service.subject_policy_service, "_load_active_user_override", lambda subject_id: (
        calls.append(f"user:{subject_id}") or None
    ))
    monkeypatch.setattr(service.subject_policy_service, "_load_active_server_override", lambda subject_id: (
        calls.append(f"server-override:{subject_id}") or None
    ))

    enriched_inputs: list[dict] = []

    def enrich(subject, **kwargs):
        calls.append(f"enrich:{subject['subject_id']}")
        enriched_inputs.append({"subject": subject, **kwargs})
        mode = "selective" if subject["subject_id"] == "persisted:known-id" else "vpn"
        return {**subject, "effective_state": {"effective_mode": mode}}

    monkeypatch.setattr(service.subject_policy_service, "enrich_subject_with_effective_state", enrich)
    monkeypatch.setattr(service, "_build_binding_for_subject", lambda subject: (
        calls.append(f"build:{subject['subject_id']}") or {"subject_id": subject["subject_id"]}
    ))
    monkeypatch.setattr(service, "_applied_handoff_assignments", lambda: calls.append("old-assignments") or [])
    monkeypatch.setattr(service, "_annotate_bindings_with_handoff", lambda values: (
        calls.append("annotate") or [{**item, "annotated": True} for item in values]
    ))
    monkeypatch.setattr(service, "build_xray_handoff_assignments", lambda values, **_kwargs: (
        calls.append("assignments") or [{"subject_id": item["subject_id"]} for item in values]
    ))

    bindings, modes, assignments = service._prospective_profile_bindings(
        [
            {"client_uuid": "known-id", "client_email": "sub-known@example.test", "xray_alias": "Known"},
            {"client_uuid": "new-id", "client_email": "sub-new@example.test", "xray_alias": "New", "server_id": "requested-server"},
        ],
        token_prefix="sub-",
        preserve_existing_overrides=True,
    )

    assert calls == [
        "bindings", "modes", "routing", "enforcement", "bypass",
        "subject:known-id", "user:persisted:known-id", "server-override:persisted:known-id",
        "enrich:persisted:known-id",
        "subject:new-id", "user:xray:new-id", "server-override:xray:new-id",
        "enrich:xray:new-id", "build:xray:new-id",
        "old-assignments", "annotate", "assignments",
    ]
    assert [item["subject_id"] for item in bindings] == ["unmanaged", "xray:new-id"]
    assert bindings[1]["annotated"] is True
    assert modes == [
        {"subject_id": "old-mode", "client_email": "other@example.test"},
        {
            "subject_id": "persisted:known-id", "client_id": "known-id", "client_uuid": "known-id",
            "client_email": "sub-known@example.test", "desired_mode": "selective",
            "effective_mode": "unsupported_selective", "mode_support_state": "unsupported_legacy",
        },
    ]
    assert assignments == [{"subject_id": "unmanaged"}, {"subject_id": "xray:new-id"}]
    assert enriched_inputs[1]["subject"]["detail"] == {
        "client_id": "new-id", "client_uuid": "new-id", "email": "sub-new@example.test", "enabled": True,
    }
    assert enriched_inputs[1]["server_override"] == {
        "subject_id": "xray:new-id", "selected_server_id": "requested-server",
        "selected_until": "2099-12-31 23:59:59", "apply_state": "pending",
    }


def test_prospective_binding_projection_respects_existing_override_presence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _seed_server("existing-target")
    observed: list[dict | None] = []
    monkeypatch.setattr(service, "collect_xray_runtime_bindings", lambda **_kwargs: [])
    monkeypatch.setattr(service, "collect_xray_client_mode_directives", lambda **_kwargs: [])
    monkeypatch.setattr(service.subject_policy_service, "get_routing_snapshot", lambda: {})
    monkeypatch.setattr(service.subject_policy_service, "build_runtime_enforcement_state", lambda: {})
    monkeypatch.setattr(service.subject_policy_service, "get_core_bypass_state", lambda: {})
    monkeypatch.setattr(service, "_xray_subject_for_client", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(service.subject_policy_service, "_load_active_user_override", lambda _id: None)
    monkeypatch.setattr(service.subject_policy_service, "_load_active_server_override", lambda _id: None)
    monkeypatch.setattr(service.subject_policy_service, "enrich_subject_with_effective_state", lambda subject, **kwargs: (
        observed.append(kwargs["server_override"]) or {**subject, "effective_state": {"effective_mode": "direct"}}
    ))
    monkeypatch.setattr(service, "_applied_handoff_assignments", lambda: [])
    monkeypatch.setattr(service, "_annotate_bindings_with_handoff", lambda values: values)
    monkeypatch.setattr(service, "build_xray_handoff_assignments", lambda values, **_kwargs: [])
    node = {"client_uuid": "new-client", "client_email": "new@example.test", "server_id": "existing-target"}

    service._prospective_profile_bindings([node], token_prefix="sub-", preserve_existing_overrides=False)
    assert observed[-1] == {
        "subject_id": "xray:new-client", "selected_server_id": "existing-target",
        "selected_until": "2099-12-31 23:59:59", "apply_state": "pending",
    }

    with db_session() as connection:
        connection.execute(
            "INSERT INTO subjects (subject_id, subject_type, subject_role, implementation_kind, stable_key, "
            "desired_mode, metadata_json) VALUES ('xray:new-client', 'explicit_external_client', 'vless_client', "
            "'xray', 'xray:new-client', 'vpn', '{}')"
        )
        connection.execute(
            "INSERT INTO subject_server_overrides (subject_id, selected_server_id) "
            "VALUES ('xray:new-client', 'existing-target')"
        )
    service._prospective_profile_bindings([node], token_prefix="sub-", preserve_existing_overrides=True)
    assert observed[-1] is None


def test_capture_generation_derived_rows_scopes_by_identity_email_and_prefix() -> None:
    _seed_subject("xray:expected", {"detail": {"client_uuid": "expected-id", "email": "known@example.test"}})
    _seed_subject("xray:managed", {"detail": {"client_uuid": "managed-id", "email": "sub-managed@example.test"}}, active=0)
    _seed_subject("xray:unrelated", {"detail": {"client_uuid": "other-id", "email": "other@example.test"}})
    with db_session() as connection:
        connection.execute(
            "INSERT INTO subject_server_overrides (subject_id) VALUES ('xray:expected')"
        )
        connection.execute(
            "INSERT INTO subject_user_overrides (subject_id, override_mode) VALUES ('xray:managed', 'direct')"
        )

    captured = service._capture_generation_derived_rows(
        managed_email_prefixes=["sub-"],
        expected_client_identities=[["expected-id", "known@example.test"]],
    )

    assert [row["subject_id"] for row in captured["subjects"]] == ["xray:expected", "xray:managed"]
    assert [row["subject_id"] for row in captured["server_overrides"]] == ["xray:expected"]
    assert [row["subject_id"] for row in captured["user_overrides"]] == ["xray:managed"]


def test_current_projection_snapshot_uses_enabled_published_scope_and_rejects_bad_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import fwrouter_api.services.vpn_auto_selection_state as selection_state

    _seed_subject("xray:live", {"detail": {"client_uuid": "live-id", "email": "live@example.test"}})
    with db_session() as connection:
        account = connection.execute(
            "INSERT INTO subscription_accounts (slug, enabled) VALUES ('enabled-account', 1)"
        ).lastrowid
        disabled_account = connection.execute(
            "INSERT INTO subscription_accounts (slug, enabled) VALUES ('disabled-account', 0)"
        ).lastrowid
        connection.execute(
            "INSERT INTO subscription_clients (account_id, token, enabled) VALUES (?, 'enabled-token', 1)",
            (account,),
        )
        connection.execute(
            "INSERT INTO subscription_clients (account_id, token, enabled) VALUES (?, 'disabled-token', 1)",
            (disabled_account,),
        )
        connection.execute(
            "INSERT INTO subscription_profile_snapshots (token, nodes_json) VALUES ('enabled-token', '[]')"
        )
        connection.execute(
            "INSERT INTO subscription_profile_snapshots (token, nodes_json) VALUES ('disabled-token', '[]')"
        )
    monkeypatch.setattr(selection_state, "read_selection_fence", lambda _connection: {
        "revision": 7, "decision_id": "decision-7", "active_server_id": "server-a",
    })

    result = service._current_xray_projection_snapshot()

    assert result["identities"] == [("live-id", "live@example.test")]
    assert [item["token"] for item in result["snapshots"]] == ["disabled-token", "enabled-token"]
    assert [item["token"] for item in result["published_snapshots"]] == ["enabled-token"]
    assert result["fence"]["revision"] == 7

    _seed_subject("xray:broken", {"detail": {"client_uuid": "", "email": "missing-id@example.test"}})
    with pytest.raises(ValueError, match="current_xray_identity_incomplete"):
        service._current_xray_projection_snapshot()


def test_current_projection_snapshot_rejects_duplicate_identity_pairs() -> None:
    _seed_subject("xray:first", {"detail": {"client_uuid": "shared-id", "email": "first@example.test"}})
    _seed_subject("xray:second", {"detail": {"client_uuid": "shared-id", "email": "first@example.test"}})

    with pytest.raises(ValueError, match="current_xray_identity_duplicate"):
        service._current_xray_projection_snapshot()


def test_current_projection_fingerprint_refreshes_source_then_selection_in_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []
    source = "source-after-snapshot"
    selection = {"routing": {"mode": "auto"}, "selection_revision": 9}
    monkeypatch.setattr(service, "_generation_source_fingerprint", lambda: calls.append("source") or source)
    monkeypatch.setattr(service, "_capture_generation_auto_selection", lambda: calls.append("selection") or selection)
    snapshot = {"identities": [("id", "email")], "snapshots": [], "fence": {"revision": 8}}

    actual = service._current_projection_fingerprint(snapshot)

    expected = hashlib.sha256(json.dumps({
        "identities": snapshot["identities"], "snapshots": snapshot["snapshots"],
        "fence": snapshot["fence"], "source": source, "selection": selection,
    }, sort_keys=True, default=str).encode()).hexdigest()
    assert calls == ["source", "selection"]
    assert actual == expected


def test_bindings_artifact_parity_canonicalizes_handoff_membership_but_hashes_raw_bytes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    import fwrouter_api.services.xray_bindings as bindings_service
    import fwrouter_api.services.xray_runtime_state as runtime_state

    path = tmp_path / "bindings.json"
    monkeypatch.setattr(runtime_state, "_xray_bindings_path", lambda: path)
    bindings = [{"subject_id": "xray:a", "client_email": "a@example.test"}]
    modes = [{"subject_id": "xray:a", "desired_mode": "vpn", "effective_mode": "vpn", "mode_support_state": "supported"}]
    expected_bindings = [{
        "subject_id": "xray:a", "client_id": None, "client_uuid": None, "client_email": "a@example.test",
        "selected_server_id": None, "selected_server_source": None, "handoff_proxy_name": None,
        "server_name": None, "server_runtime_name": None, "match_key": None, "handoff": None,
    }]
    listeners = [{"port": 1234, "subject_ids": ["xray:b", "xray:a", "xray:a"], "client_emails": ["b@example.test", "a@example.test"]}]
    monkeypatch.setattr(bindings_service, "_bindings_for_state", lambda _items: expected_bindings)
    monkeypatch.setattr(bindings_service, "get_xray_handoff_listeners", lambda _items: listeners)
    payload = {
        "bindings_version": 1, "bindings": expected_bindings,
        "client_modes": modes, "handoff_listeners": [
            {"port": 1234, "subject_ids": ["xray:a", "xray:b", "xray:a"],
             "client_emails": ["a@example.test", "b@example.test"]},
        ],
        "bindings_count": 1, "applied_count": 1, "client_modes_count": 1,
    }
    payload["bindings"][0]["status"] = "applied"
    payload["client_modes"][0]["status"] = "applied"
    raw = json.dumps(payload, sort_keys=True).encode()
    path.write_bytes(raw)

    parity, digest = service._current_bindings_artifact_parity(bindings, modes)

    assert parity is True
    assert digest == hashlib.sha256(raw).hexdigest()
    path.write_text("not json", encoding="utf-8")
    parity, digest = service._current_bindings_artifact_parity(bindings, modes)
    assert parity is False
    assert digest == ""


@pytest.mark.parametrize("case", ["normal", "virtual", "alias", "missing_identity", "missing_binding"])
def test_verify_current_public_projection_enforces_identity_binding_and_vpn_auto_rules(
    monkeypatch: pytest.MonkeyPatch, case: str,
) -> None:
    monkeypatch.setattr(service, "_capture_generation_auto_selection", lambda: {
        "routing": {"active_auto_server_id": "active-server"},
    })
    binding = {
        "client_uuid": "id-1", "client_email": "one@example.test",
        "selected_server_id": "server-a", "selected_server_source": "manual",
    }
    bindings = [binding]
    monkeypatch.setattr(service, "collect_xray_runtime_bindings", lambda: bindings)
    monkeypatch.setattr(service, "_supersession_alias_owns", lambda old, new, proof: (
        old == "old-server" and new == "server-a" and proof == {"checkpoint_sha256": "proof"}
    ))
    public_server = "server-a" if case != "alias" else "old-server"
    node = {"client_uuid": "id-1", "client_email": "one@example.test", "server_id": public_server}
    identity_pairs = [("id-1", "one@example.test")]
    if case == "missing_identity":
        identity_pairs = []
    elif case == "missing_binding":
        bindings.clear()
    elif case == "virtual":
        node["server_id"] = "virtual:xray:vpn-auto"
        binding = {**binding, "selected_server_id": "vpn-global", "selected_server_source": "vpn_auto"}
        bindings[:] = [binding]
    snapshot = {
        "identities": identity_pairs,
        "published_snapshots": [{"nodes_json": json.dumps([node])}],
    }

    result = service._verify_current_public_projection(
        snapshot,
        supersession={"checkpoint_sha256": "proof"} if case == "alias" else None,
    )

    assert result is (case in {"normal", "virtual", "alias"})


def test_supersession_alias_ownership_requires_exact_transition_proofs() -> None:
    checkpoint_sha = "a" * 64
    plan_sha = "b" * 64
    with db_session() as connection:
        connection.execute(
            "INSERT INTO subscription_node_identities "
            "(identity_id, origin_server_id, current_server_id, revision, provenance_sha256) "
            "VALUES ('identity-a', 'old-server', 'new-server', 2, ?)" ,
            ("c" * 64,),
        )
        connection.executemany(
            "INSERT INTO subscription_node_aliases (server_id, identity_id) VALUES (?, 'identity-a')",
            [("old-server",), ("new-server",)],
        )
        connection.execute(
            """INSERT INTO subscription_node_identity_transitions (
                   identity_id, from_server_id, to_server_id, from_revision, to_revision,
                   before_provenance_sha256, after_provenance_sha256, source_ownership_sha256,
                   operator_plan_sha256, checkpoint_sha256
               ) VALUES ('identity-a', 'old-server', 'new-server', 1, 2, ?, ?, ?, ?, ?)""",
            ("d" * 64, "e" * 64, "f" * 64, plan_sha, checkpoint_sha),
        )

    assert service._supersession_alias_owns(
        "old-server", "new-server", {"checkpoint_sha256": checkpoint_sha, "migration_plan_sha256": plan_sha},
    ) is True
    assert service._supersession_alias_owns(
        "old-server", "new-server", {"checkpoint_sha256": checkpoint_sha, "migration_plan_sha256": "0" * 64},
    ) is False
    assert service._supersession_alias_owns("old-server", "new-server", None) is False


def test_recovery_selection_readback_fails_closed_on_native_direct_proof_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import fwrouter_api.services.dataplane_global as dataplane_global
    import fwrouter_api.services.dataplane_live as dataplane_live
    import fwrouter_api.services.dataplane_status as dataplane_status
    import fwrouter_api.services.live_probe_cache as live_probe_cache
    import fwrouter_api.services.servers as servers_service
    import fwrouter_api.services.vpn_auto_selection_state as selection_state

    captured = {
        "routing": {
            "server_mode": "auto", "desired_fixed_server_id": None, "applied_fixed_server_id": None,
            "active_auto_server_id": None, "desired_mode": "direct", "applied_mode": "direct",
            "apply_state": "clean",
        },
        "selection_revision": 12, "selection_decision_id": None,
    }
    routing = dict(captured["routing"])
    fence = {"revision": 12, "decision_id": None, "active_server_id": None, "provenance_server_id": None}
    monkeypatch.setattr(service, "_verify_generation_selection_readback", lambda _selection: {
        "ok": False, "error_code": "SELECTION_BASELINE_UNSELECTED",
    })
    monkeypatch.setattr(servers_service, "get_routing_global_state", lambda **_kwargs: routing)
    monkeypatch.setattr(service, "_current_xray_projection_snapshot", lambda: {"fence": fence})
    monkeypatch.setattr(selection_state, "read_selection_fence", lambda _connection: fence)
    monkeypatch.setattr(dataplane_status, "_read_live_dataplane_payload", lambda: {"ready": True})
    monkeypatch.setattr(dataplane_status, "_live_owned_table_ready", lambda _payload: True)
    monkeypatch.setattr(dataplane_global, "_read_applied_manifest_uncached", lambda: {
        "routing_global_state": {"desired_mode": "direct", "applied_mode": "direct"},
    })
    monkeypatch.setattr(dataplane_live, "_probe_live_global_mode_uncached", lambda: {"mode": "direct"})
    monkeypatch.setattr(live_probe_cache, "get_live_probe_cache", lambda _key, *, loader, **_kwargs: loader())
    monkeypatch.setattr(dataplane_status, "build_runtime_enforcement_state", lambda **_kwargs: {
        "enforcement_level": "global_direct_enforced", "traffic_enforcement_guaranteed": True,
        "active_mode_matches_intent": False, "live_global_mode": "direct",
    })

    result = service._verify_generation_recovery_selection_readback(captured)

    assert result == {"ok": False, "error_code": "SELECTION_DIRECT_NATIVE_POLICY_UNPROVEN"}


def test_publication_result_builder_preserves_nonstaged_and_staged_shapes() -> None:
    from fwrouter_api.services import xray_generation_views as generation_views

    values = {
        "desired_nodes": [{
            "server_id": "server-a", "server_name": "Server A", "client_uuid": "client-a",
            "client_email": "client-a@example.test",
        }],
        "created": [{"client_uuid": "client-a"}],
        "deleted": [],
        "recreated": [],
        "reconcile_details": {"ok": True, "raw": "do-not-return"},
        "materialize_result": {"ok": True},
    }
    promotion = {"profiles_count": 1, "nodes_count": 1}
    verification = {"ok": True, "operation_id": "op-1"}

    nonstaged = generation_views.build_publication_result(values, promotion, verification)
    assert nonstaged == {
        "ok": True, "status": "success", "nodes_count": 1,
        "created_count": 1, "deleted_count": 0, "recreated_count": 0,
        "created": [{"client_uuid": "client-a"}], "deleted": [], "recreated": [],
        "client_reconcile": {"ok": True},
        "nodes": [{"server_id": "server-a", "server_name": "Server A",
                   "client_uuid": "client-a", "client_email": "client-a@example.test"}],
        "materialize": {"ok": True}, "public_profile_promote": promotion,
        "pre_publication_verification": verification,
    }
    staged = generation_views.build_publication_result(
        values, promotion, verification, generation_apply={"xray": {"ok": True}},
    )
    assert staged["generation_apply"] == {"xray": {"ok": True}}
    assert list(staged)[-2:] == ["generation_apply", "pre_publication_verification"]


def test_native_validation_proof_builder_preserves_receipt_hash_projection() -> None:
    from fwrouter_api.services.xray_generation_checkpoint import build_native_validation_proof

    staged = {
        "native_validation": {
            "xray": {
                "stage": "validated", "candidate_sha256": "x" * 64,
                "expected_client_identities": [["id-a", "a@example.test"]],
            },
            "transition": {
                "candidate_sha256": "t" * 64,
                "validation": {"ok": True, "native": {"returncode": 0}},
            },
            "final": {
                "candidate_sha256": "f" * 64,
                "validation": {"ok": False, "error_code": "NATIVE_REJECTED"},
            },
        },
    }

    result = build_native_validation_proof(staged)
    transition_bytes = json.dumps(
        staged["native_validation"]["transition"]["validation"],
        sort_keys=True, separators=(",", ":"), default=str,
    ).encode("utf-8")
    identity_bytes = json.dumps(
        [["id-a", "a@example.test"]], sort_keys=True,
        separators=(",", ":"), default=str,
    ).encode("utf-8")
    assert result == {
        "xray": {
            "ok": True, "candidate_sha256": "x" * 64,
            "expected_client_identities_sha256": hashlib.sha256(identity_bytes).hexdigest(),
        },
        "mihomo": {
            "transition": {
                "ok": True, "candidate_sha256": "t" * 64,
                "receipt_sha256": hashlib.sha256(transition_bytes).hexdigest(),
            },
            "final": {
                "ok": False, "candidate_sha256": "f" * 64,
                "receipt_sha256": hashlib.sha256(json.dumps(
                    staged["native_validation"]["final"]["validation"],
                    sort_keys=True, separators=(",", ":"), default=str,
                ).encode("utf-8")).hexdigest(),
            },
        },
    }


def test_d2_coordinator_exports_keep_canonical_component_function_identity() -> None:
    from fwrouter_api.services import (
        xray_generation_checkpoint,
        xray_generation_observation,
        xray_generation_staging,
        xray_generation_views,
    )

    assert service._vpn_auto_servers_for_xray_subscription is profile_planning.read_vpn_auto_candidates
    assert service._prospective_profile_bindings is profile_planning.prepare_profile_bindings
    assert service._compile_generation_inputs is profile_planning.compile_generation_inputs
    assert service._capture_generation_auto_selection is xray_generation_observation.capture_generation_selection
    assert service._generation_source_fingerprint is xray_generation_observation.generation_source_fingerprint
    assert service._capture_generation_derived_rows is xray_generation_views.capture_derived_rows
    assert service._current_xray_projection_snapshot is xray_generation_views.capture_current_projection
    assert service._verify_current_public_projection is xray_generation_views.verify_public_projection
    assert service._stage_profile_native_candidates is xray_generation_staging.prepare_native_candidates
    assert service._build_publication_result is xray_generation_views.build_publication_result
    assert service._build_checkpoint_native_validation_proof is (
        xray_generation_checkpoint.build_native_validation_proof
    )


def test_coordinator_compatibility_patch_updates_consumer_bindings_and_restores(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from fwrouter_api.services import (
        xray_generation_observation,
        xray_generation_staging,
        xray_generation_views,
    )

    canonical_selection = xray_generation_observation.capture_generation_selection
    canonical_projection = xray_generation_views.capture_current_projection
    selection_consumers = (
        xray_generation_observation,
        xray_generation_views,
        xray_generation_staging,
    )
    assert all(module._capture_generation_auto_selection is canonical_selection
               for module in selection_consumers)
    assert xray_generation_views._current_xray_projection_snapshot is canonical_projection
    assert xray_generation_staging._current_xray_projection_snapshot is canonical_projection

    replacement_selection = lambda: {"patched": "selection"}
    replacement_projection = lambda: {"patched": "projection"}
    with monkeypatch.context() as patch:
        patch.setattr(service, "_capture_generation_auto_selection", replacement_selection)
        patch.setattr(service, "_current_xray_projection_snapshot", replacement_projection)

        assert service._capture_generation_auto_selection is replacement_selection
        assert all(module._capture_generation_auto_selection is replacement_selection
                   for module in selection_consumers)
        assert service._current_xray_projection_snapshot is replacement_projection
        assert xray_generation_views._current_xray_projection_snapshot is replacement_projection
        assert xray_generation_staging._current_xray_projection_snapshot is replacement_projection

        # Compatibility patching is consumer-local; canonical public exports
        # remain the actual implementations throughout.
        assert xray_generation_observation.capture_generation_selection is canonical_selection
        assert xray_generation_views.capture_current_projection is canonical_projection

    assert service._capture_generation_auto_selection is canonical_selection
    assert all(module._capture_generation_auto_selection is canonical_selection
               for module in selection_consumers)
    assert service._current_xray_projection_snapshot is canonical_projection
    assert xray_generation_views._current_xray_projection_snapshot is canonical_projection
    assert xray_generation_staging._current_xray_projection_snapshot is canonical_projection
    assert xray_generation_observation.capture_generation_selection is canonical_selection
    assert xray_generation_views.capture_current_projection is canonical_projection

