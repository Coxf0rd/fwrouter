from __future__ import annotations

import json
import hashlib
from pathlib import Path
from threading import Event, Thread
from types import SimpleNamespace
from typing import Any

import pytest

from fwrouter_api.adapters.mihomo import DEFAULT_MIHOMO_ADAPTER, MihomoApplyResult
from fwrouter_api.db.connection import db_session
from fwrouter_api.services import mihomo_config as mihomo_config_service
from fwrouter_api.services.server_global_selection import (
    apply_global_auto_server,
    apply_global_fixed_server,
    clear_global_fixed_server,
    set_global_fixed_server,
    _restore_global_routing_state,
)
from fwrouter_api.services import selector as selector_service
from fwrouter_api.services.server_state import (
    ensure_routing_global_state,
    expire_global_fixed_server,
    get_routing_global_state,
)
from fwrouter_api.services.vpn_auto_selection_state import read_selection_revision


def _seed_server(server_id: str, *, vpn_auto: bool, global_list: bool) -> str:
    runtime_name = f"Runtime {server_id}"
    raw = {"name": runtime_name, "type": "socks5", "server": "198.18.0.10", "port": 12345}
    with db_session() as connection:
        connection.execute(
            """
            INSERT INTO servers(server_id, server_name, provider_name, inventory_state, raw_json)
            VALUES (?, ?, 'fixed-mode-source-test', 'active', ?)
            """,
            (server_id, runtime_name, json.dumps(raw, sort_keys=True)),
        )
        connection.execute(
            """
            INSERT INTO server_preferences(server_id, vpn_auto, vpn_auto_priority, global_list)
            VALUES (?, ?, 0, ?)
            """,
            (server_id, int(vpn_auto), int(global_list)),
        )
    return runtime_name


def test_fixed_target_outside_auto_and_global_lists_is_generated_only_for_vpn_global() -> None:
    runtime_name = _seed_server("fixed-outside-lists", vpn_auto=False, global_list=False)

    config = mihomo_config_service.build_mihomo_config(
        {
            "server_mode": "fixed",
            "desired_fixed_server_id": "fixed-outside-lists",
            "applied_fixed_server_id": "fixed-outside-lists",
            "desired_mode": "selective",
            "applied_mode": "selective",
            "selective_default": "direct",
        },
        xray_handoff_assignments=[],
    )

    proxies = {str(proxy.get("name")) for proxy in config.get("proxies", []) if isinstance(proxy, dict)}
    groups = {
        str(group.get("name")): list(group.get("proxies") or [])
        for group in config.get("proxy-groups", [])
        if isinstance(group, dict)
    }
    assert runtime_name in proxies
    assert groups["vpn-global"] == ["REJECT", runtime_name]
    assert "vpn-auto" not in groups["vpn-global"]
    assert runtime_name not in groups["vpn-auto"]


def test_fixed_logical_profile_beyond_default_inventory_limit_gets_owned_group() -> None:
    from fwrouter_api.services.custom_servers import resolve_mihomo_runtime_proxy_rows
    from fwrouter_api.services.mihomo_config_proxies import (
        _ensure_selector_groups,
        _logical_profile_groups,
        _merge_runtime_proxies,
    )

    filler_count = 1000
    filler_rows = []
    filler_preferences = []
    for index in range(filler_count):
        server_id = f"limit-filler-{index:04d}"
        server_name = f"00 filler {index:04d}"
        raw = {"name": server_name, "type": "socks5", "server": "198.18.0.10", "port": 12345}
        filler_rows.append((server_id, server_name, json.dumps(raw, sort_keys=True)))
        filler_preferences.append((server_id, 0, 1))

    fixed_id = "zz-fixed-logical-profile"
    group_name = "Fixed Logical Profile [profile123456]"
    member_identity = "member-profile-1"
    member_config = {
        "type": "vless",
        "server": "198.18.0.44",
        "port": 443,
        "uuid": "00000000-0000-4000-8000-000000000001",
        "name": "Profile member",
    }
    target_raw = {
        "name": group_name,
        "_fwrouter_runtime_name": group_name,
        "_fwrouter_topology": {
            "kind": "logical_profile",
            "runtime_policy": "fallback",
            "endpoints": [{"identity": member_identity, "runtime": member_config}],
        },
    }
    with db_session() as connection:
        connection.executemany(
            "INSERT INTO servers(server_id, server_name, provider_name, inventory_state, raw_json) "
            "VALUES (?, ?, 'synthetic-fixed-profile-test', 'active', ?)",
            filler_rows,
        )
        connection.executemany(
            "INSERT INTO server_preferences(server_id, vpn_auto, global_list) VALUES (?, ?, ?)",
            filler_preferences,
        )
        connection.execute(
            "INSERT INTO servers(server_id, server_name, provider_name, inventory_state, raw_json) "
            "VALUES (?, ?, 'synthetic-fixed-profile-test', 'active', ?)",
            (fixed_id, group_name, json.dumps(target_raw, sort_keys=True)),
        )
        connection.execute(
            "INSERT INTO server_preferences(server_id, vpn_auto, global_list) VALUES (?, 0, 0)",
            (fixed_id,),
        )

    assert not any(group.get("name") == group_name for group in _logical_profile_groups())
    explicit_rows = resolve_mihomo_runtime_proxy_rows(
        inventory_state="active",
        limit=1000,
        explicit_server_ids={fixed_id},
    )
    assert any(row.get("server_id") == fixed_id for row in explicit_rows)

    routing = {
        "server_mode": "fixed",
        "desired_fixed_server_id": fixed_id,
        "applied_fixed_server_id": fixed_id,
        "desired_mode": "selective",
        "applied_mode": "selective",
        "selective_default": "direct",
    }
    config = {"proxies": _merge_runtime_proxies({}, routing=routing), "proxy-groups": []}
    groups = {
        str(group.get("name")): group
        for group in _ensure_selector_groups(config, routing=routing)
        if isinstance(group, dict)
    }
    expected_member = f"{group_name} :: {member_identity[:12]}"
    assert groups[group_name]["proxies"] == [expected_member]
    assert any(proxy.get("name") == expected_member for proxy in config["proxies"])
    assert groups["vpn-global"]["proxies"] == ["REJECT", group_name]
    assert group_name not in groups["vpn-auto"]["proxies"]
    assert expected_member not in groups["vpn-auto"]["proxies"]


@pytest.mark.parametrize(
    "invalid_members",
    [
        ["REJECT", "Runtime fixed-membership", "vpn-auto"],
        ["Runtime fixed-membership"],
        ["REJECT", "Runtime different-target"],
    ],
)
def test_fixed_selector_validation_requires_exact_failclosed_target_membership(invalid_members: list[str]) -> None:
    _seed_server("fixed-membership", vpn_auto=False, global_list=False)
    routing = {
        "server_mode": "fixed",
        "desired_fixed_server_id": "fixed-membership",
        "applied_fixed_server_id": "fixed-membership",
        "desired_mode": "selective",
        "applied_mode": "selective",
        "selective_default": "direct",
    }
    config = mihomo_config_service.build_mihomo_config(routing, xray_handoff_assignments=[])
    from fwrouter_api.services.mihomo_config_validation import _validate_candidate_structure

    assert _validate_candidate_structure(config, routing=routing)["fixed_global_membership_ok"] is True
    groups = {str(group.get("name")): group for group in config.get("proxy-groups", []) if isinstance(group, dict)}
    groups["vpn-global"]["proxies"] = invalid_members
    assert _validate_candidate_structure(config, routing=routing)["fixed_global_membership_ok"] is False


def test_targetless_fixed_direct_keeps_legacy_group_without_fixed_proof() -> None:
    runtime_name = _seed_server("fixed-direct-legacy", vpn_auto=True, global_list=True)
    routing = {
        "server_mode": "fixed",
        "desired_fixed_server_id": None,
        "applied_fixed_server_id": None,
        "desired_mode": "direct",
        "applied_mode": "direct",
        "selective_default": "direct",
    }
    config = mihomo_config_service.build_mihomo_config(routing, xray_handoff_assignments=[])
    from fwrouter_api.services.mihomo_config_validation import _validate_candidate_structure

    structure = _validate_candidate_structure(config, routing=routing)
    groups = {
        str(group.get("name")): list(group.get("proxies") or [])
        for group in config.get("proxy-groups", [])
        if isinstance(group, dict)
    }
    assert groups["vpn-global"] == ["vpn-auto", runtime_name, "REJECT"]
    assert structure["fixed_targetless_direct_compat"] is True
    assert structure["fixed_targetless_intent_supported"] is True
    assert structure["fixed_global_membership_required"] is False
    assert structure["fixed_global_membership_ok"] is False
    assert structure["selector_groups_ok"] is True


@pytest.mark.parametrize("mode", ["selective", "vpn"])
def test_targetless_fixed_non_direct_candidate_is_fail_closed_and_rejected(mode: str) -> None:
    _seed_server(f"fixed-no-target-{mode}", vpn_auto=True, global_list=True)
    routing = {
        "server_mode": "fixed",
        "desired_fixed_server_id": None,
        "applied_fixed_server_id": None,
        "desired_mode": mode,
        "applied_mode": mode,
        "selective_default": "vpn" if mode == "vpn" else "direct",
    }
    config = mihomo_config_service.build_mihomo_config(routing, xray_handoff_assignments=[])
    from fwrouter_api.services.mihomo_config_validation import _validate_candidate_structure

    structure = _validate_candidate_structure(config, routing=routing)
    groups = {
        str(group.get("name")): list(group.get("proxies") or [])
        for group in config.get("proxy-groups", [])
        if isinstance(group, dict)
    }
    assert groups["vpn-global"] == ["REJECT"]
    assert structure["fixed_targetless_direct_compat"] is False
    assert structure["fixed_targetless_intent_supported"] is False
    assert structure["fixed_global_membership_ok"] is False
    assert structure["selector_groups_ok"] is False


def test_fixed_routing_status_requires_live_ttl_exact_membership_and_selection(
    monkeypatch, tmp_path: Path,
) -> None:
    import yaml

    from fwrouter_api.services import mihomo_config_paths, mihomo_config_status

    runtime_name = "Runtime fixed-native-proof"
    _seed_server("fixed-native-proof", vpn_auto=False, global_list=False)
    with db_session() as connection:
        connection.execute(
            "UPDATE servers SET raw_json=? WHERE server_id=?",
            (json.dumps({
                "name": runtime_name,
                "type": "socks5",
                "server": "198.18.0.11",
                "port": 12345,
                "_fwrouter_runtime_name": runtime_name,
            }), "fixed-native-proof"),
        )
    selected = set_global_fixed_server("fixed-native-proof", requested_by="pytest")
    assert selected["ok"] is True
    with db_session() as connection:
        connection.execute(
            """UPDATE routing_global_state
               SET applied_fixed_server_id=desired_fixed_server_id,
                   apply_state='clean', fixed_server_until=datetime('now', '+1 day')
               WHERE id=1"""
        )
    routing = get_routing_global_state(expire_ttl=False)
    config = mihomo_config_service.build_mihomo_config(routing, xray_handoff_assignments=[])
    config_path = tmp_path / "fixed-status.yaml"
    config_path.write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")
    monkeypatch.setattr(mihomo_config_paths, "_resolved_base_config_path", lambda: str(config_path))
    monkeypatch.setattr(mihomo_config_status, "_resolved_base_config_path", lambda: str(config_path))

    groups = {
        str(group.get("name")): list(group.get("proxies") or [])
        for group in config.get("proxy-groups", [])
        if isinstance(group, dict)
    }
    runtime_readback = {"vpn-global-now": runtime_name}

    def health():
        current = runtime_readback["vpn-global-now"]
        return SimpleNamespace(
            runtime_state="running",
            active_server_id=current,
            details={
                "selectors": {
                    "vpn_global_exists": True,
                    "vpn_global_has_vpn_auto": "vpn-auto" in groups.get("vpn-global", []),
                    "vpn_global_targets": list(groups.get("vpn-global", [])),
                    "vpn_global_now": current,
                    "vpn_global_selected_target": current,
                    "vpn_auto_targets": list(groups.get("vpn-auto", [])),
                    "vpn_auto_now": "REJECT",
                    "vpn_auto_selected_target": "REJECT",
                    "subject_selector_targets": {},
                },
                "config": {"fwrouter_contours": {"transparent_vpn": {
                    "transparent_tcp_ready": True,
                    "transparent_udp_ready": True,
                    "transparent_tcp_listener_socket_present": True,
                    "transparent_udp_listener_socket_present": True,
                }}},
            },
        )

    monkeypatch.setattr(mihomo_config_status.DEFAULT_MIHOMO_ADAPTER, "health", health)
    valid = mihomo_config_status.mihomo_runtime_satisfies_routing(routing)
    assert valid["ok"] is True, valid
    assert valid["vpn_egress_ready"] is True

    runtime_readback["vpn-global-now"] = "REJECT"
    unselected = mihomo_config_status.mihomo_runtime_satisfies_routing(routing)
    assert unselected["ok"] is False
    assert unselected["selector_ok"] is False

    runtime_readback["vpn-global-now"] = runtime_name
    groups["vpn-global"].insert(1, "vpn-auto")
    extra_member = mihomo_config_status.mihomo_runtime_satisfies_routing(routing)
    assert extra_member["ok"] is False
    assert extra_member["selector_native_membership_ok"] is False

    with db_session() as connection:
        connection.execute("UPDATE routing_global_state SET fixed_server_until='garbage' WHERE id=1")
    malformed_ttl = mihomo_config_status.mihomo_runtime_satisfies_routing(routing)
    assert malformed_ttl["ok"] is False
    assert malformed_ttl["selector_ok"] is False


def test_same_fixed_target_request_reserves_a_new_selection_revision() -> None:
    _seed_server("fixed-same-target", vpn_auto=False, global_list=False)

    first = set_global_fixed_server("fixed-same-target", requested_by="pytest")
    with db_session() as connection:
        first_revision = read_selection_revision(connection)
    second = set_global_fixed_server("fixed-same-target", requested_by="pytest")
    with db_session() as connection:
        second_revision = read_selection_revision(connection)

    assert first["ok"] is True
    assert second["ok"] is True
    assert second_revision == first_revision + 1
    assert second["selection_revision"] == second_revision


def test_stale_fixed_failure_rollback_cannot_replace_newer_auto_intent() -> None:
    _seed_server("fixed-stale-rollback", vpn_auto=False, global_list=False)
    fixed = set_global_fixed_server("fixed-stale-rollback", requested_by="pytest")
    previous = {
        "desired_mode": "direct",
        "applied_mode": "direct",
        "selective_default": "direct",
        "server_mode": "auto",
        "desired_fixed_server_id": None,
        "applied_fixed_server_id": None,
        "fixed_server_until": None,
        "apply_state": "clean",
        "error_code": None,
        "error_message": None,
    }
    clear = clear_global_fixed_server(requested_by="pytest")

    rollback = _restore_global_routing_state(
        previous,
        expected_revision=fixed["selection_revision"],
        expected_server_id="fixed-stale-rollback",
        expected_fixed_until=str(fixed["routing"]["fixed_server_until"]),
    )

    routing = get_routing_global_state(expire_ttl=False)
    assert clear["ok"] is True
    assert rollback["ok"] is False
    assert rollback["error_code"] == "GLOBAL_FIXED_SERVER_STALE"
    assert routing["server_mode"] == "auto"
    assert routing["desired_fixed_server_id"] is None


def test_fixed_ttl_expiry_advances_selection_fence_without_runtime_apply() -> None:
    _seed_server("fixed-expired", vpn_auto=False, global_list=False)
    set_global_fixed_server("fixed-expired", requested_by="pytest")
    with db_session() as connection:
        connection.execute(
            """UPDATE routing_global_state
               SET applied_fixed_server_id='fixed-expired', apply_state='clean',
                   fixed_server_until=datetime('now', '-1 second') WHERE id=1"""
        )
        revision_before = read_selection_revision(connection)

    result = expire_global_fixed_server(dry_run=False, apply_runtime=False)

    routing = get_routing_global_state(expire_ttl=False)
    with db_session() as connection:
        revision_after = read_selection_revision(connection)
    assert result["expired_global_fixed_server_count"] == 1
    assert result["runtime_apply"]["skipped"] is True
    assert routing["server_mode"] == "auto"
    assert routing["apply_state"] == "pending"
    assert revision_after > revision_before


@pytest.mark.parametrize("drift", ["core_revision", "source", "runtime_incarnation", "core_context"])
def test_fixed_apply_token_rejects_core_source_and_runtime_drift(monkeypatch, drift: str) -> None:
    _seed_server(f"fixed-token-{drift}", vpn_auto=False, global_list=False)
    from fwrouter_api.services import core_intent, mihomo_runtime, server_global_selection

    runtime_context = {
        "boot_id": "00000000-0000-0000-0000-000000000001",
        "network_namespace_inode": "1",
    }
    incarnation = {"value": "fixed-token-incarnation"}
    monkeypatch.setattr(core_intent, "read_core_runtime_context", lambda: dict(runtime_context))
    monkeypatch.setattr(
        mihomo_runtime,
        "get_mihomo_runtime_incarnation",
        lambda **_kwargs: incarnation["value"],
    )
    desired = set_global_fixed_server(f"fixed-token-{drift}", requested_by="pytest")
    assert desired["ok"] is True
    token = server_global_selection._capture_fixed_apply_context(desired["routing"])
    assert isinstance(token, dict)
    assert server_global_selection._fixed_apply_token_matches(
        server_id=f"fixed-token-{drift}",
        fixed_until=str(desired["routing"]["fixed_server_until"]),
        token=token,
    ) is True

    if drift == "core_revision":
        with db_session() as connection:
            from fwrouter_api.services.core_intent import advance_core_intent_revision

            advance_core_intent_revision(connection)
    elif drift == "source":
        with db_session() as connection:
            connection.execute(
                "UPDATE servers SET server_name='Changed source row' WHERE server_id=?",
                (f"fixed-token-{drift}",),
            )
    elif drift == "runtime_incarnation":
        incarnation["value"] = "new-runtime-incarnation"
    else:
        runtime_context["network_namespace_inode"] = "2"

    assert server_global_selection._fixed_apply_token_matches(
        server_id=f"fixed-token-{drift}",
        fixed_until=str(desired["routing"]["fixed_server_until"]),
        token=token,
    ) is False


@pytest.mark.parametrize("receipt_case", ["missing", "wrong_operation"])
def test_fixed_apply_rejects_missing_or_mismatched_generation_receipt(
    monkeypatch, receipt_case: str,
) -> None:
    server_id = f"fixed-bad-receipt-{receipt_case}"
    _seed_server(server_id, vpn_auto=False, global_list=False)
    from fwrouter_api.adapters import mihomo as mihomo_adapter_module
    from fwrouter_api.services import core_intent, mihomo_runtime, server_global_selection

    monkeypatch.setattr(core_intent, "read_core_runtime_context", lambda: {
        "boot_id": "00000000-0000-0000-0000-000000000001",
        "network_namespace_inode": "1",
    })
    monkeypatch.setattr(mihomo_runtime, "get_mihomo_runtime_incarnation", lambda **_kwargs: "bad-receipt-runtime")
    native_calls: list[tuple[str, str]] = []
    monkeypatch.setattr(
        mihomo_adapter_module,
        "DEFAULT_MIHOMO_ADAPTER",
        SimpleNamespace(
            get_active_server_id=lambda: "REJECT",
            apply_server_to_selector=lambda selector, target: native_calls.append((selector, target)),
        ),
    )

    def invalid_receipt(routing, *, token, job_id):
        del routing, token
        operation_id = "actual-operation"
        return {
            "ok": True,
            "operation_id": operation_id,
            "job_id": job_id,
            "reentry_generation_fence": None if receipt_case == "missing" else {
                "schema": "mihomo-reentry-generation-fence/v1",
                "operation_id": "different-operation",
                "job_id": job_id,
                "before_revision": 1,
                "selection_revision": 1,
                "runtime_incarnation": "bad-receipt-runtime",
                "active_config_hash": "0" * 64,
                "native_proof_verified": True,
                "publication_verified": True,
                "unchanged_config": True,
            },
        }

    monkeypatch.setattr(server_global_selection, "_materialize_fixed_server_generation", invalid_receipt)
    result = apply_global_fixed_server(server_id, requested_by="pytest")

    assert result["ok"] is False
    assert result["error_code"] == "MIHOMO_RECONCILE_FAILED"
    assert result["mihomo_reconcile"]["error_code"] == "MIHOMO_GENERATION_RECEIPT_INVALID"
    assert native_calls == []
    assert get_routing_global_state(expire_ttl=False)["apply_state"] == "pending"


def test_malformed_fixed_ttl_is_rejected_by_proof_token_and_restore(monkeypatch) -> None:
    _seed_server("fixed-malformed-ttl", vpn_auto=False, global_list=False)
    selected = set_global_fixed_server("fixed-malformed-ttl", requested_by="pytest")
    assert selected["ok"] is True
    with db_session() as connection:
        connection.execute(
            """UPDATE routing_global_state
               SET applied_fixed_server_id=desired_fixed_server_id,
                   fixed_server_until='garbage', apply_state='clean'
               WHERE id=1"""
        )
        revision = read_selection_revision(connection)
        from fwrouter_api.services.core_intent import read_core_intent_revision

        core_revision = read_core_intent_revision(connection)
        core_row = connection.execute(
            "SELECT desired_state, lifecycle_mode, apply_state FROM modules WHERE module_name='core'"
        ).fetchone()
        core_snapshot = tuple(str(core_row[key]) for key in (
            "desired_state", "lifecycle_mode", "apply_state",
        ))

    routing = get_routing_global_state(expire_ttl=False)
    from fwrouter_api.services.mihomo_config_status import _fixed_intent_is_clean_and_unexpired
    from fwrouter_api.services.server_global_selection import _fixed_apply_token_matches
    from fwrouter_api.services import core_intent, mihomo_runtime

    assert _fixed_intent_is_clean_and_unexpired(routing) is False
    monkeypatch.setattr(core_intent, "read_core_runtime_context", lambda: {
        "boot_id": "00000000-0000-0000-0000-000000000001",
        "network_namespace_inode": "1",
    })
    monkeypatch.setattr(mihomo_runtime, "get_mihomo_runtime_incarnation", lambda **_kwargs: "synthetic-incarnation")
    assert _fixed_apply_token_matches(
        server_id="fixed-malformed-ttl",
        fixed_until="garbage",
        token={
            "selection_revision": revision,
            "core_revision": core_revision,
            "core_snapshot": core_snapshot,
            "core_runtime_context": {
                "boot_id": "00000000-0000-0000-0000-000000000001",
                "network_namespace_inode": "1",
            },
            "runtime_incarnation": "synthetic-incarnation",
        },
    ) is False

    class Operations:
        def list_servers(self):
            return []

        def apply_server_to_selector(self, selector_name: str, target: str):
            pytest.fail(f"malformed fixed TTL must not write {selector_name}={target}")

    adapter = {
        "adapter_id": "synthetic-mihomo",
        "capabilities": [
            selector_service.RUNTIME_CAPABILITY_LIST_SERVERS,
            selector_service.RUNTIME_CAPABILITY_APPLY_SELECTOR,
            selector_service.RUNTIME_CAPABILITY_HEALTH,
        ],
    }
    monkeypatch.setattr(selector_service, "_active_selector_runtime", lambda: (adapter, Operations()))
    monkeypatch.setattr(
        selector_service,
        "_runtime_health_or_error",
        lambda *_args, **_kwargs: (SimpleNamespace(runtime_state="running"), None),
    )
    result = selector_service.restore_mihomo_selector_state(
        routing=get_routing_global_state(expire_ttl=False),
    )
    assert result["ok"] is False
    assert result.get("blocked") is True
    assert result.get("skip_reason") == "fixed_routing_intent_unsettled"


@pytest.mark.parametrize("auto_commits_during_postcheck", [True, False])
def test_fixed_completion_cannot_overwrite_a_newer_auto_clear_or_native_drift(
    monkeypatch,
    tmp_path: Path,
    auto_commits_during_postcheck: bool,
) -> None:
    runtime_name = _seed_server("fixed-clear-race", vpn_auto=False, global_list=True)
    state: dict[str, str] = {"vpn-global": "vpn-auto"}
    native_put_done = Event()
    allow_fixed_apply_return = Event()
    clear_started = Event()
    clear_done = Event()
    post_check_started = Event()
    allow_post_check_finish = Event()
    thread_errors: list[BaseException] = []
    fixed_result: dict[str, Any] = {}
    clear_result: dict[str, Any] = {}

    import yaml

    from fwrouter_api.services import mihomo_config_status
    from fwrouter_api.services import mihomo_reconcile
    from fwrouter_api.services import mihomo_runtime
    from fwrouter_api.services import mihomo_config_paths
    from fwrouter_api.services import core_intent

    active_config = tmp_path / "config.yaml"
    synthetic_routing = {
        "server_mode": "fixed",
        "desired_fixed_server_id": "fixed-clear-race",
        "applied_fixed_server_id": "fixed-clear-race",
        "desired_mode": "selective",
        "applied_mode": "selective",
        "selective_default": "direct",
    }
    config = mihomo_config_service.build_mihomo_config(synthetic_routing, xray_handoff_assignments=[])
    active_config.write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")
    monkeypatch.setattr(mihomo_config_paths, "_resolved_base_config_path", lambda: active_config)
    monkeypatch.setattr(mihomo_config_status, "_resolved_base_config_path", lambda: active_config)
    monkeypatch.setattr(core_intent, "read_core_runtime_context", lambda: {
        "boot_id": "00000000-0000-0000-0000-000000000001",
        "network_namespace_inode": "1",
    })
    monkeypatch.setattr(mihomo_runtime, "get_mihomo_runtime_incarnation", lambda **_kwargs: "synthetic-incarnation")

    def fake_health():
        groups = {
            str(group.get("name")): list(group.get("proxies") or [])
            for group in config.get("proxy-groups", [])
            if isinstance(group, dict)
        }
        return SimpleNamespace(
            runtime_state="running",
            active_server_id=state["vpn-global"],
            details={"selectors": {
                "vpn_global_exists": True,
                "vpn_global_has_vpn_auto": "vpn-auto" in groups.get("vpn-global", []),
                "vpn_global_targets": groups.get("vpn-global", []),
                "vpn_auto_targets": groups.get("vpn-auto", []),
                "vpn_auto_now": "REJECT" if groups.get("vpn-auto") == ["REJECT"] else None,
                "vpn_auto_selected_target": "REJECT" if groups.get("vpn-auto") == ["REJECT"] else None,
                "vpn_global_now": state["vpn-global"],
                "vpn_global_selected_target": state["vpn-global"],
            }, "config": {"fwrouter_contours": {"transparent_vpn": {
                "transparent_tcp_ready": True,
                "transparent_udp_ready": True,
                "transparent_tcp_listener_socket_present": True,
                "transparent_udp_listener_socket_present": True,
            }}}},
        )

    monkeypatch.setattr(DEFAULT_MIHOMO_ADAPTER, "health", fake_health)

    def synthetic_generation(*, routing, job_id, verification_callback, generation_materialization_callback=None):
        nonlocal config
        if routing.get("server_mode") == "fixed":
            assert callable(generation_materialization_callback)
            assert generation_materialization_callback is not verification_callback
        config = mihomo_config_service.build_mihomo_config(routing, xray_handoff_assignments=[])
        active_config.write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")
        if str(routing.get("server_mode") or "auto").strip().lower() == "fixed":
            state["vpn-global"] = "REJECT"
        with db_session() as connection:
            expected_revision = read_selection_revision(connection)
        operation_id = "synthetic-generation"
        verification = verification_callback(
            operation_id=operation_id,
            expected_selection_revision=expected_revision,
        )
        return {
            "ok": bool(verification.get("ok")),
            "operation_id": operation_id,
            "job_id": job_id,
            "reconcile_reason": "input_fingerprint_unchanged",
            "verification_callback_result": verification,
            "reentry_generation_fence": {
                "schema": "mihomo-reentry-generation-fence/v1",
                "operation_id": operation_id,
                "job_id": job_id,
                "before_revision": expected_revision,
                "selection_revision": expected_revision,
                "runtime_incarnation": "synthetic-incarnation",
                "active_config_hash": hashlib.sha256(active_config.read_bytes()).hexdigest(),
                "native_proof_verified": True,
                "publication_verified": True,
                "unchanged_config": True,
            },
        }

    monkeypatch.setattr(mihomo_reconcile, "reconcile_mihomo_runtime", synthetic_generation)
    def fake_get_active_server_id() -> str:
        return state["vpn-global"]

    def fake_apply_server_to_selector(selector_name: str, server_id: str) -> MihomoApplyResult:
        assert selector_name == "vpn-global"
        state[selector_name] = server_id
        result = MihomoApplyResult(
            ok=True,
            message="synthetic native selector readback matched",
            active_server_id=server_id,
            details={"selector_after": server_id},
        )
        if server_id == runtime_name:
            native_put_done.set()
            if not allow_fixed_apply_return.wait(timeout=5):
                raise TimeoutError("bounded synthetic interleaving was not released")
        return result

    monkeypatch.setattr(DEFAULT_MIHOMO_ADAPTER, "get_active_server_id", fake_get_active_server_id)
    monkeypatch.setattr(DEFAULT_MIHOMO_ADAPTER, "apply_server_to_selector", fake_apply_server_to_selector)
    ping_count = 0

    def fake_check_server_delay(server_id: str, **_kwargs):
        nonlocal ping_count
        ping_count += 1
        if ping_count == 2:
            if not auto_commits_during_postcheck:
                # Simulate an unowned selector change during the unlocked probe.
                state["vpn-global"] = "vpn-auto"
            post_check_started.set()
            if not allow_post_check_finish.wait(timeout=5):
                raise TimeoutError("bounded synthetic post-check was not released")
        return {
            "ok": True,
            "server_id": server_id,
            "status": "success",
            "error_code": None,
            "error_message": None,
        }

    monkeypatch.setattr("fwrouter_api.services.server_ping.check_server_delay", fake_check_server_delay)

    def fixed_apply_thread() -> None:
        try:
            fixed_result.update(
                apply_global_fixed_server(
                    "fixed-clear-race",
                    requested_by="pytest",
                )
            )
        except BaseException as exc:  # bounded handoff to deterministic main-thread assertion
            thread_errors.append(exc)

    def auto_clear_thread() -> None:
        clear_started.set()
        try:
            clear_result.update(apply_global_auto_server(requested_by="pytest"))
        except BaseException as exc:
            thread_errors.append(exc)
        finally:
            clear_done.set()

    fixed_thread = Thread(target=fixed_apply_thread, name="fixed-mode-clear-race")
    clear_thread = Thread(target=auto_clear_thread, name="fixed-mode-newer-auto-clear")
    fixed_thread.start()
    try:
        assert native_put_done.wait(timeout=5), (
            "fixed native PUT did not reach the controlled barrier; "
            f"result={fixed_result!r}; thread_errors={thread_errors!r}"
        )
        with db_session() as connection:
            revision_after_fixed_intent = read_selection_revision(connection)

        if auto_commits_during_postcheck:
            clear_thread.start()
            assert clear_started.wait(timeout=5)
            clear_was_serialized = not clear_done.wait(timeout=0.2)
        else:
            clear_was_serialized = True

        allow_fixed_apply_return.set()
        assert post_check_started.wait(timeout=5), "fixed apply skipped the unlocked post-check window"
        if auto_commits_during_postcheck:
            assert clear_done.wait(timeout=5), "newer auto clear did not finish during the fixed post-check"
        allow_post_check_finish.set()
        fixed_thread.join(timeout=5)
        if clear_thread.ident is not None:
            clear_thread.join(timeout=5)

        assert not fixed_thread.is_alive(), "fixed apply thread exceeded the join bound"
        assert not clear_thread.is_alive(), "auto clear thread exceeded the join bound"
        assert not thread_errors, f"selection thread failed: {thread_errors!r}"
        assert clear_was_serialized is True
        assert fixed_result["ok"] is False
        assert fixed_result["error_code"] == (
            "GLOBAL_FIXED_SERVER_STALE"
            if auto_commits_during_postcheck
            else "GLOBAL_FIXED_SERVER_NATIVE_PROOF_FAILED"
        )
        if auto_commits_during_postcheck:
            assert clear_result["ok"] is True, clear_result
            assert clear_result["routing_after"]["server_mode"] == "auto"
        assert state["vpn-global"] == "vpn-auto"
        with db_session() as connection:
            revision_after_clear = read_selection_revision(connection)
        if auto_commits_during_postcheck:
            assert revision_after_clear > revision_after_fixed_intent
            assert clear_result["routing_after"]["applied_fixed_server_id"] is None
        else:
            assert revision_after_clear == revision_after_fixed_intent
            assert get_routing_global_state(expire_ttl=False)["apply_state"] == "pending"
    finally:
        allow_fixed_apply_return.set()
        allow_post_check_finish.set()
        fixed_thread.join(timeout=5)
        if clear_thread.ident is not None:
            clear_thread.join(timeout=5)


def test_newer_auto_intent_wins_before_fixed_generation_starts(monkeypatch, tmp_path: Path) -> None:
    _seed_server("fixed-before-generation-race", vpn_auto=False, global_list=False)
    from fwrouter_api.adapters import mihomo as mihomo_adapter_module
    from fwrouter_api.services import core_intent, mihomo_reconcile, mihomo_runtime, server_global_selection

    monkeypatch.setattr(core_intent, "read_core_runtime_context", lambda: {
        "boot_id": "00000000-0000-0000-0000-000000000001",
        "network_namespace_inode": "1",
    })
    monkeypatch.setattr(mihomo_runtime, "get_mihomo_runtime_incarnation", lambda **_kwargs: "pre-generation-runtime")
    monkeypatch.setattr(
        mihomo_adapter_module,
        "DEFAULT_MIHOMO_ADAPTER",
        SimpleNamespace(get_active_server_id=lambda: "vpn-auto", apply_server_to_selector=lambda *_args: pytest.fail("stale generation must not select")),
    )

    materialization_captured = Event()
    allow_generation_check = Event()
    reconcile_calls: list[dict[str, Any]] = []
    original_materializer = server_global_selection._materialize_fixed_server_generation

    def delayed_materializer(routing, *, token, job_id):
        materialization_captured.set()
        if not allow_generation_check.wait(timeout=5):
            raise TimeoutError("bounded pre-generation race was not released")
        return original_materializer(routing, token=token, job_id=job_id)

    monkeypatch.setattr(server_global_selection, "_materialize_fixed_server_generation", delayed_materializer)
    monkeypatch.setattr(
        mihomo_reconcile,
        "reconcile_mihomo_runtime",
        lambda **kwargs: reconcile_calls.append(kwargs) or {"ok": False},
    )
    fixed_result: dict[str, Any] = {}
    thread_errors: list[BaseException] = []

    def fixed_apply_thread() -> None:
        try:
            fixed_result.update(apply_global_fixed_server("fixed-before-generation-race", requested_by="pytest"))
        except BaseException as exc:
            thread_errors.append(exc)

    thread = Thread(target=fixed_apply_thread, name="fixed-pre-generation-race")
    thread.start()
    try:
        assert materialization_captured.wait(timeout=5), "fixed apply did not capture intent before generation"
        auto_result = clear_global_fixed_server(requested_by="pytest-newer-auto")
        allow_generation_check.set()
        thread.join(timeout=5)
        assert not thread.is_alive(), "fixed apply thread exceeded the join bound"
        assert not thread_errors, f"fixed apply thread failed: {thread_errors!r}"
        assert auto_result["ok"] is True
        assert fixed_result["ok"] is False, fixed_result
        assert fixed_result["error_code"] == "GLOBAL_FIXED_SERVER_STALE"
        assert reconcile_calls == []
        routing = get_routing_global_state(expire_ttl=False)
        assert routing["server_mode"] == "auto"
        assert routing["desired_fixed_server_id"] is None
        assert routing["applied_fixed_server_id"] is None
    finally:
        allow_generation_check.set()
        thread.join(timeout=5)


def test_auto_clear_cannot_enter_while_fixed_reconcile_effect_window_is_open(monkeypatch, tmp_path: Path) -> None:
    _seed_server("fixed-reconcile-window-race", vpn_auto=False, global_list=False)
    from fwrouter_api.adapters import mihomo as mihomo_adapter_module
    from fwrouter_api.adapters.xray_common import xray_writer_guard_is_held
    from fwrouter_api.services import core_intent, mihomo_reconcile, mihomo_runtime

    monkeypatch.setattr(core_intent, "read_core_runtime_context", lambda: {
        "boot_id": "00000000-0000-0000-0000-000000000001",
        "network_namespace_inode": "1",
    })
    monkeypatch.setattr(mihomo_runtime, "get_mihomo_runtime_incarnation", lambda **_kwargs: "reconcile-window-runtime")
    monkeypatch.setattr(
        mihomo_adapter_module,
        "DEFAULT_MIHOMO_ADAPTER",
        SimpleNamespace(
            get_active_server_id=lambda: "vpn-auto",
            apply_server_to_selector=lambda *_args: pytest.fail("failed reconcile must not select"),
        ),
    )

    reconcile_entered = Event()
    allow_reconcile_finish = Event()
    clear_started = Event()
    clear_done = Event()
    reconcile_guard_held: list[bool] = []
    fixed_result: dict[str, Any] = {}
    clear_result: dict[str, Any] = {}
    thread_errors: list[BaseException] = []

    def paused_reconcile(**_kwargs):
        reconcile_guard_held.append(xray_writer_guard_is_held())
        reconcile_entered.set()
        if not allow_reconcile_finish.wait(timeout=5):
            raise TimeoutError("bounded reconcile window was not released")
        return {"ok": False, "error_code": "SYNTHETIC_RECONCILE_STOP"}

    monkeypatch.setattr(mihomo_reconcile, "reconcile_mihomo_runtime", paused_reconcile)

    def fixed_apply_thread() -> None:
        try:
            fixed_result.update(apply_global_fixed_server("fixed-reconcile-window-race", requested_by="pytest"))
        except BaseException as exc:
            thread_errors.append(exc)

    def auto_clear_thread() -> None:
        clear_started.set()
        try:
            clear_result.update(clear_global_fixed_server(requested_by="pytest-newer-auto"))
        except BaseException as exc:
            thread_errors.append(exc)
        finally:
            clear_done.set()

    fixed_thread = Thread(target=fixed_apply_thread, name="fixed-reconcile-window")
    fixed_thread.start()
    clear_thread: Thread | None = None
    try:
        assert reconcile_entered.wait(timeout=5), "fixed reconcile owner was not entered"
        clear_thread = Thread(target=auto_clear_thread, name="auto-clear-during-fixed-reconcile")
        clear_thread.start()
        assert clear_started.wait(timeout=5), "newer auto clear did not start"
        assert not clear_done.wait(timeout=0.1), "auto clear entered while fixed reconcile effect window was open"
    finally:
        allow_reconcile_finish.set()
        fixed_thread.join(timeout=5)
        if clear_thread is not None:
            clear_thread.join(timeout=5)

    assert not fixed_thread.is_alive(), "fixed apply exceeded the join bound"
    assert clear_thread is not None and not clear_thread.is_alive(), "auto clear exceeded the join bound"
    assert not thread_errors, f"selection thread failed: {thread_errors!r}"
    assert reconcile_guard_held == [True]
    assert fixed_result["ok"] is False, fixed_result
    assert clear_result["ok"] is True, clear_result
    routing = get_routing_global_state(expire_ttl=False)
    assert routing["server_mode"] == "auto"
    assert routing["desired_fixed_server_id"] is None
    assert routing["applied_fixed_server_id"] is None


def test_pending_fixed_restore_does_not_replay_stale_applied_target(monkeypatch) -> None:
    _seed_server("fixed-old-applied", vpn_auto=False, global_list=True)
    _seed_server("fixed-new-desired", vpn_auto=False, global_list=True)
    ensure_routing_global_state()
    with db_session() as connection:
        connection.execute(
            """
            UPDATE routing_global_state
            SET server_mode='fixed', desired_fixed_server_id='fixed-new-desired',
                applied_fixed_server_id='fixed-old-applied',
                fixed_server_until=datetime('now', '+1 day'), apply_state='pending',
                error_code=NULL, error_message=NULL
            WHERE id=1
            """
        )

    calls: list[tuple[str, str]] = []

    class Operations:
        def list_servers(self):
            return []

        def apply_server_to_selector(self, selector_name: str, target: str):
            calls.append((selector_name, target))
            return SimpleNamespace(ok=True, to_dict=lambda: {"ok": True, "active_server_id": target})

    operations = Operations()
    adapter = {
        "adapter_id": "synthetic-mihomo",
        "capabilities": [
            selector_service.RUNTIME_CAPABILITY_LIST_SERVERS,
            selector_service.RUNTIME_CAPABILITY_APPLY_SELECTOR,
            selector_service.RUNTIME_CAPABILITY_HEALTH,
        ],
    }
    monkeypatch.setattr(selector_service, "_active_selector_runtime", lambda: (adapter, operations))
    from fwrouter_api.services import core_intent, mihomo_runtime
    monkeypatch.setattr(core_intent, "read_core_runtime_context", lambda: {
        "boot_id": "00000000-0000-0000-0000-000000000001",
        "network_namespace_inode": "1",
    })
    monkeypatch.setattr(mihomo_runtime, "get_mihomo_runtime_incarnation", lambda **_kwargs: "synthetic-incarnation")
    monkeypatch.setattr(
        selector_service,
        "_runtime_health_or_error",
        lambda *_args, **_kwargs: (SimpleNamespace(runtime_state="running"), None),
    )
    monkeypatch.setattr(selector_service, "_load_selector_candidates", lambda: [])

    result = selector_service.restore_mihomo_selector_state(
        routing=get_routing_global_state(expire_ttl=False)
    )

    assert result["ok"] is False, result
    assert result.get("blocked") is True
    assert result.get("skip_reason") == "fixed_routing_intent_unsettled", result
    assert calls == []
