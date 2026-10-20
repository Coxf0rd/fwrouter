from __future__ import annotations

import json
import os
from types import SimpleNamespace
from pathlib import Path

import yaml
import pytest

from fwrouter_api.adapters.mihomo import NON_SERVER_PROXY_TARGETS
from fwrouter_api.core.config import get_settings
from fwrouter_api.db.connection import db_session, initialize_database
from fwrouter_api.services import mihomo_config as mihomo_config_service
from fwrouter_api.services import mihomo_config_inbounds as mihomo_inbounds_service
from fwrouter_api.services import mihomo_config_proxies as mihomo_proxies_service
from fwrouter_api.services import mihomo_reconcile as mihomo_reconcile_service
from fwrouter_api.services import subscription_profiles as subscription_profiles_service
from fwrouter_api.services import xray as xray_service
from fwrouter_api.services import xray_subscription_service
from fwrouter_api.services import xray_runtime_state as xray_runtime_state_service
from fwrouter_api.services.live_probe_cache import (
    clear_live_probe_cache,
    get_live_probe_cache,
    peek_live_probe_cache,
)
from fwrouter_api.services.mihomo_reconcile_fingerprint import (
    current_mihomo_input_fingerprint,
    write_mihomo_reconcile_fingerprint_state,
)
from fwrouter_api.services import mihomo_reconcile_fingerprint as mihomo_reconcile_fingerprint_service
from fwrouter_api.services import mihomo_config_status as mihomo_config_status_service
from fwrouter_api.services.vpn_auto_selection_state import advance_selection_revision, read_selection_revision
from fwrouter_api.adapters.xray_common import xray_writer_guard


def _configure_env(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("FWROUTER_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setattr(
        mihomo_config_service,
        "_resolve_transparent_bind_address",
        lambda: mihomo_config_service.TRANSPARENT_BIND_ADDRESS,
    )
    get_settings.cache_clear()


def _write_yaml(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")


def _write_effective_rules(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    clear_live_probe_cache()


def _enable_vpn_module() -> None:
    with db_session() as connection:
        connection.execute("UPDATE modules SET desired_state = 'enabled' WHERE module_name = 'vpn'")


@pytest.mark.parametrize(
    ("field", "bad_value", "missing"),
    [
        ("resolved_selective_default", "vpn", False),
        ("final_match_rule", "MATCH,vpn-global", False),
        ("transparent_final_match_rule", "MATCH,DIRECT", False),
        ("resolved_selective_default", None, True),
        ("final_match_rule", None, True),
        ("transparent_final_match_rule", None, True),
    ],
)
def test_mihomo_runtime_rejects_scalar_metadata_mismatch_before_loading_rules_or_native_health(
    monkeypatch,
    tmp_path: Path,
    field: str,
    bad_value: str | None,
    missing: bool,
) -> None:
    _configure_env(monkeypatch, tmp_path)
    routing = {
        "desired_mode": "vpn",
        "applied_mode": "vpn",
        "selective_default": "direct",
        "server_mode": "auto",
    }
    metadata = {
        "resolved_selective_default": "direct",
        "final_match_rule": "MATCH,DIRECT",
        "transparent_final_match_rule": "MATCH,vpn-global",
    }
    if missing:
        del metadata[field]
    else:
        metadata[field] = bad_value
    monkeypatch.setattr(mihomo_config_status_service, "_scan_fwrouter_config_metadata", lambda _path: metadata)

    def unexpected(_path):
        raise AssertionError("metadata mismatch must reject before loading the full YAML")

    monkeypatch.setattr(mihomo_config_status_service, "_safe_load_yaml", unexpected)
    monkeypatch.setattr(
        mihomo_config_status_service,
        "_build_mihomo_config_with_source",
        lambda _routing: (_ for _ in ()).throw(AssertionError("metadata mismatch must not rebuild 200k rules")),
    )
    monkeypatch.setattr(
        mihomo_config_status_service.DEFAULT_MIHOMO_ADAPTER,
        "health",
        lambda: (_ for _ in ()).throw(AssertionError("rejected metadata must not call native health")),
    )

    result = mihomo_config_status_service.mihomo_runtime_satisfies_routing(routing)

    assert result == {
        "ok": False,
        "reason": "fwrouter_metadata_mismatch",
        "metadata": metadata,
        "expected": {
            "resolved_selective_default": "direct",
            "final_match_rule": "MATCH,DIRECT",
            "transparent_final_match_rule": "MATCH,vpn-global",
            "transparent_udp_policy_ok": None,
            "full_vpn_udp_policy_ok": None,
        },
    }


def test_mihomo_runtime_matching_scalar_metadata_keeps_policy_and_native_checks(
    monkeypatch,
    tmp_path: Path,
) -> None:
    _configure_env(monkeypatch, tmp_path)
    routing = {
        "desired_mode": "vpn",
        "applied_mode": "vpn",
        "selective_default": "direct",
        "server_mode": "auto",
    }
    transparent_rules = ["MATCH,vpn-global", "NETWORK,udp,REJECT"]
    full_vpn_rules = ["MATCH,vpn-global", "NETWORK,udp,REJECT"]
    metadata = {
        "resolved_selective_default": "direct",
        "final_match_rule": "MATCH,DIRECT",
        "transparent_final_match_rule": "MATCH,vpn-global",
    }
    config = {
        "sub-rules": {
            "fwrouter-transparent": transparent_rules,
            "fwrouter-full-vpn": full_vpn_rules,
        },
        "proxy-groups": [
            {"name": "vpn-auto", "proxies": ["REJECT"]},
            {"name": "vpn-global", "proxies": ["vpn-auto", "REJECT"]},
        ],
    }
    health_calls: list[bool] = []
    monkeypatch.setattr(mihomo_config_status_service, "_scan_fwrouter_config_metadata", lambda _path: metadata)
    monkeypatch.setattr(mihomo_config_status_service, "_safe_load_yaml", lambda _path: config)
    monkeypatch.setattr(
        mihomo_config_status_service,
        "_build_mihomo_config_with_source",
        lambda _routing: ([], {
            "transparent_rules": transparent_rules,
            "full_vpn_rules": full_vpn_rules,
        }),
    )
    monkeypatch.setattr(
        mihomo_config_status_service,
        "_ensure_selector_groups",
        lambda active, **_kwargs: active["proxy-groups"],
    )
    monkeypatch.setattr(
        mihomo_config_status_service.DEFAULT_MIHOMO_ADAPTER,
        "health",
        lambda: health_calls.append(True) or SimpleNamespace(
            runtime_state="running",
            active_server_id=None,
            details={
                "selectors": {
                    "vpn_global_exists": True,
                    "vpn_global_has_vpn_auto": True,
                    "vpn_global_now": "vpn-auto",
                    "vpn_auto_targets": ["REJECT"],
                    "vpn_auto_now": "REJECT",
                    "vpn_global_targets": ["vpn-auto", "REJECT"],
                    "subject_selector_targets": {},
                },
                "config": {"fwrouter_contours": {"transparent_vpn": {
                    "transparent_tcp_ready": True,
                    "transparent_udp_ready": True,
                    "transparent_tcp_listener_socket_present": True,
                    "transparent_udp_listener_socket_present": True,
                }}},
            },
        ),
    )

    result = mihomo_config_status_service.mihomo_runtime_satisfies_routing(routing)

    assert result["ok"] is True
    assert result["metadata_ok"] is True
    assert result["selector_native_membership_ok"] is True
    assert result["transparent_ok"] is True
    assert health_calls == [True]


@pytest.mark.parametrize(
    ("case", "expected_ok"),
    [
        ("logical_profile_runtime_name", True),
        ("ordinary_proxy_name", True),
        ("unknown_logical_id", False),
        ("missing_logical_id", False),
        ("duplicate_runtime_alias", False),
        ("global_only_target", False),
        ("stale_native_target", False),
        ("native_logical_id", False),
        ("empty_reject_pool", True),
        ("fixed_mode", False),
    ],
)
def test_mihomo_runtime_resolves_auto_logical_target_to_unique_generated_name(
    monkeypatch,
    tmp_path: Path,
    case: str,
    expected_ok: bool,
) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()
    logical_id = "sub:logical-vpn-target"
    runtime_name = "Logical VPN group [a1b2c3d4]"
    empty_pool = case == "empty_reject_pool"
    unknown_id = case == "unknown_logical_id"
    if not empty_pool and not unknown_id:
        _seed_runtime_proxy_server(
            logical_id, server_name="Logical VPN", vpn_auto=case != "global_only_target", global_list=True,
        )
        raw = {"name": runtime_name, "type": "socks5", "server": "203.0.113.12", "port": 1080}
        if case != "ordinary_proxy_name":
            raw["_fwrouter_runtime_name"] = runtime_name
        if case not in {"ordinary_proxy_name", "duplicate_runtime_alias"}:
            raw["_fwrouter_topology"] = {
                "kind": "logical_profile", "runtime_policy": "fallback",
                "endpoints": [{"identity": "sub:fixture-member", "runtime": {
                    "type": "socks5", "server": "203.0.113.13", "port": 1080,
                }}],
            }
        with db_session() as connection:
            connection.execute(
                "UPDATE servers SET raw_json = ? WHERE server_id = ?",
                (json.dumps(raw), logical_id),
            )
            if case == "missing_logical_id":
                connection.execute("UPDATE servers SET inventory_state = 'missing' WHERE server_id = ?", (logical_id,))
        if case == "duplicate_runtime_alias":
            _seed_runtime_proxy_server(
                "other:duplicate-target", server_name="Other VPN", vpn_auto=True, global_list=True,
            )
            with db_session() as connection:
                connection.execute(
                    "UPDATE servers SET raw_json = ? WHERE server_id = ?",
                    (json.dumps(raw), "other:duplicate-target"),
                )
        if case == "global_only_target":
            other_runtime_name = "Independent VPN target"
            _seed_runtime_proxy_server(
                "other:eligible-target", server_name=other_runtime_name, vpn_auto=True, global_list=True,
            )

    routing = {
        "desired_mode": "selective", "applied_mode": "selective",
        "selective_default": "vpn", "server_mode": "fixed" if case == "fixed_mode" else "auto",
        "active_auto_server_id": None if empty_pool else "sub:unknown-target" if unknown_id else logical_id,
    }
    transparent_rules = ["MATCH,vpn-global", "NETWORK,udp,REJECT"]
    full_vpn_rules = ["MATCH,vpn-global", "NETWORK,udp,REJECT"]
    metadata = {
        "resolved_selective_default": "vpn",
        "final_match_rule": "MATCH,DIRECT",
        "transparent_final_match_rule": "MATCH,vpn-global",
    }
    runtime_proxies = []
    if case == "ordinary_proxy_name":
        runtime_proxies.append({
            "name": runtime_name, "type": "socks5", "server": "203.0.113.12", "port": 1080,
        })
    if case == "global_only_target":
        runtime_proxies.append({
            "name": "Independent VPN target", "type": "socks5", "server": "203.0.113.14", "port": 1080,
        })
    config = {
        "proxies": runtime_proxies,
        "sub-rules": {
            "fwrouter-transparent": transparent_rules,
            "fwrouter-full-vpn": full_vpn_rules,
        },
        "proxy-groups": [],
    }
    config["proxy-groups"] = mihomo_config_status_service._ensure_selector_groups(config, routing=routing)
    groups_by_name = {group["name"]: group for group in config["proxy-groups"]}
    auto_targets = groups_by_name["vpn-auto"]["proxies"]
    global_targets = groups_by_name["vpn-global"]["proxies"]
    native_now = {
        "stale_native_target": "stale-native-name",
        "native_logical_id": logical_id,
        "empty_reject_pool": "REJECT",
        "global_only_target": "Independent VPN target",
    }.get(case, runtime_name)
    monkeypatch.setattr(mihomo_config_status_service, "_scan_fwrouter_config_metadata", lambda _path: metadata)
    monkeypatch.setattr(mihomo_config_status_service, "_safe_load_yaml", lambda _path: config)
    monkeypatch.setattr(
        mihomo_config_status_service,
        "_build_mihomo_config_with_source",
        lambda _routing: ([], {
            "transparent_rules": transparent_rules,
            "full_vpn_rules": full_vpn_rules,
        }),
    )
    monkeypatch.setattr(
        mihomo_config_status_service.DEFAULT_MIHOMO_ADAPTER,
        "health",
        lambda: SimpleNamespace(
            runtime_state="running",
            active_server_id=runtime_name,
            details={
                "selectors": {
                    "vpn_global_exists": True,
                    "vpn_global_has_vpn_auto": True,
                    "vpn_global_now": "vpn-auto",
                    "vpn_auto_targets": auto_targets,
                    "vpn_auto_now": native_now,
                    "vpn_auto_selected_target": native_now,
                    "vpn_global_targets": global_targets,
                    "subject_selector_targets": {},
                },
                "config": {"fwrouter_contours": {"transparent_vpn": {
                    "transparent_tcp_ready": True,
                    "transparent_udp_ready": True,
                    "transparent_tcp_listener_socket_present": True,
                    "transparent_udp_listener_socket_present": True,
                }}},
            },
        ),
    )

    result = mihomo_config_status_service.mihomo_runtime_satisfies_routing(routing)

    assert result["ok"] is expected_ok
    assert result["selector_ok"] is expected_ok
    assert result["selector_native_membership_ok"] is True


@pytest.mark.parametrize(
    ("proxy_name", "is_reserved"),
    [(name, True) for name in sorted(NON_SERVER_PROXY_TARGETS)] + [("reject", False)],
)
def test_mihomo_runtime_distinguishes_reserved_alias_from_custom_proxy_name(
    monkeypatch,
    tmp_path: Path,
    proxy_name: str,
    is_reserved: bool,
) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()
    selected_id = f"custom-https:{proxy_name.lower()}"
    other_id = "ordinary:eligible-vpn"
    other_name = "Independent eligible VPN"
    _seed_runtime_proxy_server(
        selected_id, server_name=proxy_name, vpn_auto=True, global_list=True,
    )
    _seed_runtime_proxy_server(other_id, server_name=other_name, vpn_auto=True, global_list=True)
    with db_session() as connection:
        connection.execute(
            "INSERT INTO server_custom_https_proxy (server_id, proxy_type, host, port) "
            "VALUES (?, 'socks5', 'proxy.example.test', 1080)",
            (selected_id,),
        )

    routing = {
        "desired_mode": "selective", "applied_mode": "selective",
        "selective_default": "vpn", "server_mode": "auto",
        "active_auto_server_id": selected_id,
    }
    transparent_rules = ["MATCH,vpn-global", "NETWORK,udp,REJECT"]
    full_vpn_rules = ["MATCH,vpn-global", "NETWORK,udp,REJECT"]
    config = {
        "proxies": [
            {"name": proxy_name, "type": "socks5", "server": "203.0.113.20", "port": 1080},
            {"name": other_name, "type": "socks5", "server": "203.0.113.21", "port": 1080},
        ],
        "sub-rules": {
            "fwrouter-transparent": transparent_rules,
            "fwrouter-full-vpn": full_vpn_rules,
        },
        "proxy-groups": [],
    }
    config["proxy-groups"] = mihomo_config_status_service._ensure_selector_groups(config)
    groups_by_name = {group["name"]: group for group in config["proxy-groups"]}
    auto_targets = groups_by_name["vpn-auto"]["proxies"]
    global_targets = groups_by_name["vpn-global"]["proxies"]
    assert other_name in auto_targets
    assert len(auto_targets) >= 3
    metadata = {
        "resolved_selective_default": "vpn",
        "final_match_rule": "MATCH,DIRECT",
        "transparent_final_match_rule": "MATCH,vpn-global",
    }
    monkeypatch.setattr(mihomo_config_status_service, "_scan_fwrouter_config_metadata", lambda _path: metadata)
    monkeypatch.setattr(mihomo_config_status_service, "_safe_load_yaml", lambda _path: config)
    monkeypatch.setattr(
        mihomo_config_status_service,
        "_build_mihomo_config_with_source",
        lambda _routing: ([], {
            "transparent_rules": transparent_rules,
            "full_vpn_rules": full_vpn_rules,
        }),
    )
    monkeypatch.setattr(
        mihomo_config_status_service.DEFAULT_MIHOMO_ADAPTER,
        "health",
        lambda: SimpleNamespace(
            runtime_state="running",
            active_server_id=proxy_name,
            details={
                "selectors": {
                    "vpn_global_exists": True,
                    "vpn_global_has_vpn_auto": True,
                    "vpn_global_now": "vpn-auto",
                    "vpn_auto_targets": auto_targets,
                    "vpn_auto_now": proxy_name,
                    "vpn_auto_selected_target": proxy_name,
                    "vpn_global_targets": global_targets,
                    "subject_selector_targets": {},
                },
                "config": {"fwrouter_contours": {"transparent_vpn": {
                    "transparent_tcp_ready": True,
                    "transparent_udp_ready": True,
                    "transparent_tcp_listener_socket_present": True,
                    "transparent_udp_listener_socket_present": True,
                }}},
            },
        ),
    )

    result = mihomo_config_status_service.mihomo_runtime_satisfies_routing(routing)

    assert result["ok"] is (not is_reserved)
    assert result["selector_ok"] is (not is_reserved)
    assert result["selector_native_membership_ok"] is True


def _seed_runtime_proxy_server(
    server_id: str,
    *,
    server_name: str,
    vpn_auto: bool,
    global_list: bool,
    vpn_auto_priority: int = 0,
) -> None:
    with db_session() as connection:
        connection.execute(
            """
            INSERT INTO servers (
                server_id,
                server_name,
                provider_name,
                raw_json,
                inventory_state
            )
            VALUES (?, ?, 'pytest', ?, 'active')
            """,
            (
                server_id,
                server_name,
                json.dumps(
                    {
                        "name": server_name,
                        "type": "socks5",
                        "server": "203.0.113.10",
                        "port": 1080,
                    },
                    ensure_ascii=False,
                ),
            ),
        )
        connection.execute(
            """
            INSERT INTO server_preferences (
                server_id,
                vpn_auto,
                vpn_auto_priority,
                global_list
            )
            VALUES (?, ?, ?, ?)
            """,
            (server_id, 1 if vpn_auto else 0, vpn_auto_priority, 1 if global_list else 0),
        )


def test_fixed_logical_profile_materialization_requires_owned_group_and_native_membership(
    monkeypatch, tmp_path: Path,
) -> None:
    from fwrouter_api.services import mihomo_config_paths

    _configure_env(monkeypatch, tmp_path)
    initialize_database()
    fixed_id = "fixed-logical-proof"
    group_name = "Fixed Logical Proof [a1b2c3d4e5f6]"
    member_identity = "endpoint-proof-1"
    member_name = f"{group_name} :: {member_identity[:12]}"
    raw = {
        "name": group_name,
        "_fwrouter_runtime_name": group_name,
        "_fwrouter_topology": {
            "kind": "logical_profile",
            "runtime_policy": "fallback",
            "endpoints": [{
                "identity": member_identity,
                "runtime": {
                    "type": "vless",
                    "server": "198.18.0.44",
                    "port": 443,
                    "uuid": "00000000-0000-4000-8000-000000000002",
                    "name": "Endpoint proof",
                },
            }],
        },
    }
    with db_session() as connection:
        connection.execute(
            "INSERT INTO servers(server_id, server_name, provider_name, inventory_state, raw_json) "
            "VALUES (?, ?, 'pytest', 'active', ?)",
            (fixed_id, group_name, json.dumps(raw, sort_keys=True)),
        )
        connection.execute(
            "INSERT INTO server_preferences(server_id, vpn_auto, global_list) VALUES (?, 0, 0)",
            (fixed_id,),
        )

    routing = {
        "server_mode": "fixed",
        "desired_fixed_server_id": fixed_id,
        "applied_fixed_server_id": fixed_id,
        "desired_mode": "selective",
        "applied_mode": "selective",
        "selective_default": "direct",
        "apply_state": "pending",
    }
    config = mihomo_config_service.build_mihomo_config(routing, xray_handoff_assignments=[])
    base_path = tmp_path / "managed-mihomo.yaml"
    _write_yaml(base_path, config)
    monkeypatch.setattr(mihomo_config_paths, "_resolved_base_config_path", lambda: str(base_path))
    monkeypatch.setattr(mihomo_config_status_service, "_resolved_base_config_path", lambda: str(base_path))
    native_members = [{"runtime_identity": member_name}]
    monkeypatch.setattr(
        mihomo_config_status_service.DEFAULT_MIHOMO_ADAPTER,
        "health",
        lambda: SimpleNamespace(
            runtime_state="running",
            details={"selectors": {
                "vpn_global_exists": True,
                "vpn_global_has_vpn_auto": False,
                "vpn_global_targets": ["REJECT", group_name],
                "vpn_auto_targets": ["REJECT"],
                # Real adapter hides non-server REJECT from active identity,
                # while retaining the raw independently observed selection.
                "vpn_global_now": None,
                "vpn_global_selected_target": "REJECT",
            }},
        ),
    )
    group_readbacks: list[str] = []

    def read_groups(targets: list[str]) -> list[dict[str, object]]:
        group_readbacks.extend(targets)
        return [{
            "ok": True,
            "logical_runtime_target": group_name,
            "members": native_members,
            "effective_member_runtime_identity": member_name,
        }]

    monkeypatch.setattr(
        mihomo_config_status_service.DEFAULT_MIHOMO_ADAPTER,
        "get_logical_groups_state",
        read_groups,
    )

    proof = mihomo_config_status_service.mihomo_runtime_fixed_target_materialized(routing)

    assert proof["ok"] is True, proof
    assert proof["fixed_runtime_target"] == group_name
    assert proof["logical_group_native_membership_ok"] is True
    assert proof["selected_fixed_target"] is False
    assert proof["vpn_global_now"] == "REJECT"
    assert group_readbacks == [group_name]

    native_members[:] = [{"runtime_identity": "unexpected-member"}]
    wrong_native_membership = mihomo_config_status_service.mihomo_runtime_fixed_target_materialized(routing)
    assert wrong_native_membership["ok"] is False
    assert wrong_native_membership["logical_group_native_membership_ok"] is False


def _base_candidate() -> dict:
    return {
        "allow-lan": True,
        "routing-mark": 512,
        "bind-address": mihomo_config_service.TRANSPARENT_BIND_ADDRESS,
        "rules": ["MATCH,DIRECT"],
        "proxies": [{"name": "Test VPN", "type": "socks5", "server": "1.1.1.1", "port": 1080}],
        "proxy-groups": [
            {"name": "vpn-auto", "type": "select", "proxies": ["REJECT"]},
            {"name": "vpn-global", "type": "select", "proxies": ["vpn-auto", "REJECT"]},
        ],
        "listeners": [
            {
                "name": "fwrouter-mixed",
                "type": "mixed",
                "listen": "127.0.0.1",
                "port": 5201,
                "proxy": "vpn-global",
            },
            {
                "name": "fwrouter-redir",
                "type": "redir",
                "listen": "0.0.0.0",
                "port": 5202,
                "proxy": "vpn-global",
            },
            {
                "name": "fwrouter-tproxy",
                "type": "tproxy",
                "listen": "0.0.0.0",
                "port": 5203,
                "proxy": "vpn-global",
                "udp": True,
            },
        ],
        "sub-rules": {
            "fwrouter-transparent": ["MATCH,vpn-global", "NETWORK,udp,REJECT"],
            "fwrouter-full-vpn": ["MATCH,vpn-global", "NETWORK,udp,REJECT"],
        },
    }


def _assert_managed_contour(config: dict) -> None:
    assert config["bind-address"] == mihomo_config_service.TRANSPARENT_BIND_ADDRESS
    assert config["allow-lan"] is True
    assert config["ipv6"] is False
    assert config["routing-mark"] == 512
    assert sum(1 for listener in config["listeners"] if listener["name"] == "fwrouter-mixed") == 1
    assert sum(1 for listener in config["listeners"] if listener["name"] == "fwrouter-redir") == 1
    assert sum(1 for listener in config["listeners"] if listener["name"] == "fwrouter-tproxy") == 1
    assert sum(1 for listener in config["listeners"] if listener["name"] == "fwrouter-full-redir") == 1
    assert sum(1 for listener in config["listeners"] if listener["name"] == "fwrouter-full-tproxy") == 1
    assert config["fwrouter"]["transparent_mechanism"] == "split_redir_tproxy_ports"
    assert config["fwrouter"]["transparent_listener_name"] == "fwrouter-tproxy"
    assert config["fwrouter"]["transparent_listener_bind"] == "0.0.0.0"
    assert config["fwrouter"]["transparent_redir_port"] == 5202
    assert config["fwrouter"]["transparent_tproxy_port"] == 5203
    assert config["fwrouter"]["full_vpn_redir_port"] == 5204
    assert config["fwrouter"]["full_vpn_tproxy_port"] == 5205
    assert config["fwrouter"]["transparent_listener_port"] == 5203
    assert config["fwrouter"]["transparent_inbound_rules"] == []
    managed = {listener["name"]: listener for listener in config["listeners"]}
    assert managed["fwrouter-redir"]["rule"] == "fwrouter-transparent"
    assert "proxy" not in managed["fwrouter-redir"]
    assert managed["fwrouter-tproxy"]["rule"] == "fwrouter-transparent"
    assert "proxy" not in managed["fwrouter-tproxy"]
    assert managed["fwrouter-full-redir"]["rule"] == "fwrouter-full-vpn"
    assert managed["fwrouter-full-redir"]["port"] == 5204
    assert "proxy" not in managed["fwrouter-full-redir"]
    assert managed["fwrouter-full-tproxy"]["rule"] == "fwrouter-full-vpn"
    assert managed["fwrouter-full-tproxy"]["port"] == 5205
    assert managed["fwrouter-full-tproxy"]["udp"] is True
    assert "proxy" not in managed["fwrouter-full-tproxy"]
    sniffer = config["sniffer"]
    assert sniffer["enable"] is True
    assert sniffer["force-dns-mapping"] is True
    assert sniffer["parse-pure-ip"] is True
    assert sniffer["override-destination"] is True
    assert sniffer["sniff"]["HTTP"]["override-destination"] is True
    assert sniffer["sniff"]["TLS"]["override-destination"] is True
    assert sniffer["sniff"]["QUIC"]["override-destination"] is True


def test_build_mihomo_config_uses_direct_fallback_by_default(monkeypatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()
    monkeypatch.setattr(mihomo_config_service, "_collect_xray_handoff_assignments", lambda: [])

    config = mihomo_config_service.build_mihomo_config()

    _assert_managed_contour(config)
    assert config["rules"][-1] == "MATCH,DIRECT"
    assert config["sub-rules"]["fwrouter-transparent"][-1] == "MATCH,DIRECT"
    assert config["fwrouter"]["resolved_selective_default"] == "direct"
    assert config["fwrouter"]["final_match_rule"] == "MATCH,DIRECT"
    assert config["fwrouter"]["transparent_final_match_rule"] == "MATCH,DIRECT"
    assert config["external-controller"] == mihomo_config_service.MIHOMO_CONTROLLER_ADDRESS


def test_build_mihomo_config_does_not_add_scoped_vpn_source_rule(
    monkeypatch,
    tmp_path: Path,
) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()
    monkeypatch.setattr(mihomo_config_service, "_collect_xray_handoff_assignments", lambda: [])

    with db_session() as connection:
        connection.execute(
            """
            INSERT INTO subjects (
                subject_id,
                subject_type,
                stable_key,
                display_name,
                desired_mode,
                applied_mode,
                runtime_state,
                is_active,
                is_deleted
            )
            VALUES (
                'lan:pixel',
                'lan',
                'lan:pixel',
                'Pixel',
                'vpn',
                'vpn',
                'active',
                1,
                0
            )
            """
        )
        connection.execute(
            """
            INSERT INTO subject_lan (
                subject_id,
                mac_address,
                ip_address,
                hostname,
                dhcp_hostname
            )
            VALUES ('lan:pixel', 'aa:bb:cc:dd:ee:ff', '192.168.0.71', 'Pixel', 'Pixel')
            """
        )

    config = mihomo_config_service.build_mihomo_config({"selective_default": "direct"})

    transparent_rules = config["sub-rules"]["fwrouter-transparent"]
    assert "SRC-IP-CIDR,192.168.0.71/32,vpn-global" not in transparent_rules
    assert not any(rule.startswith("SRC-IP-CIDR,192.168.0.71/32,") for rule in transparent_rules)
    assert not any(rule.startswith("SRC-IP,") for rule in transparent_rules)
    assert transparent_rules[-1] == "MATCH,DIRECT"
    assert config["fwrouter"]["scoped_vpn_source_rules_count"] == 0


def test_build_mihomo_config_scopes_subject_server_override_to_vpn_rules_only(
    monkeypatch,
    tmp_path: Path,
) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()
    monkeypatch.setattr(mihomo_config_service, "_collect_xray_handoff_assignments", lambda: [])
    _seed_runtime_proxy_server("srv-armenia", server_name="Armenia", vpn_auto=False, global_list=True)
    settings = get_settings()
    _write_effective_rules(
        settings.paths.generated_dir / "rules" / "effective-rules.json",
        {
            "selective_default": "direct",
            "rules": [
                {"action": "DIRECT", "kind": "domain_suffix", "value": ".ru"},
                {"action": "VPN", "kind": "domain_suffix", "value": ".instagram.com"},
                {"action": "VPN", "kind": "cidr", "value": "203.0.113.0/24"},
            ],
        },
    )

    with db_session() as connection:
        connection.execute(
            """
            INSERT INTO subjects (
                subject_id,
                subject_type,
                stable_key,
                display_name,
                desired_mode,
                applied_mode,
                runtime_state,
                is_active,
                is_deleted
            )
            VALUES (
                'lan:desktop',
                'lan',
                'lan:desktop',
                'Desktop',
                'global',
                'selective',
                'active',
                1,
                0
            )
            """
        )
        connection.execute(
            """
            INSERT INTO subject_lan (
                subject_id,
                mac_address,
                ip_address,
                hostname,
                dhcp_hostname
            )
            VALUES ('lan:desktop', '74:56:3c:31:3e:38', '192.168.0.51', 'Desktop', 'Desktop')
            """
        )
        connection.execute(
            """
            INSERT INTO subject_server_overrides (
                subject_id,
                selected_server_id,
                apply_state
            )
            VALUES ('lan:desktop', 'srv-armenia', 'clean')
            """
        )

    config = mihomo_config_service.build_mihomo_config({"selective_default": "direct"})

    transparent_rules = config["sub-rules"]["fwrouter-transparent"]
    selector_name = mihomo_config_service.subject_selector_name("lan:desktop")
    assert transparent_rules[:2] == [
        f"AND,((SRC-IP-CIDR,192.168.0.51/32),(DOMAIN-SUFFIX,instagram.com)),{selector_name}",
        "AND,((NETWORK,udp),(AND,((SRC-IP-CIDR,192.168.0.51/32),(DOMAIN-SUFFIX,instagram.com)))),REJECT",
    ]
    assert transparent_rules[2:4] == [
        f"AND,((SRC-IP-CIDR,192.168.0.51/32),(IP-CIDR,203.0.113.0/24)),{selector_name}",
        "AND,((NETWORK,udp),(AND,((SRC-IP-CIDR,192.168.0.51/32),(IP-CIDR,203.0.113.0/24)))),REJECT",
    ]
    assert f"AND,((SRC-IP-CIDR,192.168.0.51/32),(DOMAIN-SUFFIX,ru)),{selector_name}" not in transparent_rules
    assert not any(rule.startswith("SRC-IP-CIDR,192.168.0.51/32,") for rule in transparent_rules)
    assert transparent_rules[-1] == "MATCH,DIRECT"
    actual_full_vpn_rules = config["sub-rules"]["fwrouter-full-vpn"]
    expected_full_vpn_rules = [
        f"SRC-IP-CIDR,192.168.0.51/32,{selector_name}",
        "AND,((NETWORK,udp),(SRC-IP-CIDR,192.168.0.51/32)),REJECT",
        "MATCH,vpn-global",
        "NETWORK,udp,REJECT",
    ]
    assert actual_full_vpn_rules == expected_full_vpn_rules, (
        f"unexpected full-vpn sub-rules: {actual_full_vpn_rules!r}"
    )
    selector_group = next(group for group in config["proxy-groups"] if group["name"] == selector_name)
    assert "Armenia" in selector_group["proxies"]
    assert "vpn-global" in selector_group["proxies"]
    assert config["fwrouter"]["scoped_vpn_source_rules_count"] == 2


def test_build_mihomo_config_sanitizes_legacy_inbound_ports_and_managed_listeners(
    monkeypatch,
    tmp_path: Path,
) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()
    monkeypatch.setattr(mihomo_config_service, "_collect_xray_handoff_assignments", lambda: [])

    settings = get_settings()
    _write_yaml(
        settings.paths.generated_dir / "mihomo" / "config.yaml",
        {
            "allow-lan": False,
            "mixed-port": 7890,
            "port": 7891,
            "socks-port": 7892,
            "redir-port": 7893,
            "tproxy-port": 9997,
            "listeners": [
                {
                    "name": "fwrouter-mixed",
                    "type": "mixed",
                    "listen": "0.0.0.0",
                    "port": 9999,
                    "proxy": "DIRECT",
                },
                {
                    "name": "fwrouter-tproxy",
                    "type": "tproxy",
                    "listen": "127.0.0.1",
                    "port": 9998,
                    "rule": "legacy-transparent",
                },
            ],
            "proxies": [{"name": "Test VPN", "type": "socks5", "server": "1.1.1.1", "port": 1080}],
            "proxy-groups": [{"name": "vpn-global", "type": "select", "proxies": ["Test VPN", "DIRECT"]}],
        },
    )

    config = mihomo_config_service.build_mihomo_config()

    for key in ("mixed-port", "port", "socks-port", "redir-port", "tproxy-port"):
        assert key not in config
    _assert_managed_contour(config)
    assert "fwrouter-mixed" in config["fwrouter"]["sanitized_managed_listeners"]
    assert "fwrouter-tproxy" in {listener["name"] for listener in config["listeners"]}


def test_rebuilding_from_active_mihomo_config_changes_only_sanitization_provenance(
    monkeypatch, tmp_path: Path,
) -> None:
    """A second proof candidate must distinguish runtime policy from sanitize history."""
    _configure_env(monkeypatch, tmp_path)
    initialize_database()
    monkeypatch.setattr(mihomo_config_service, "_collect_xray_handoff_assignments", lambda: [])

    base_path = get_settings().paths.generated_dir / "mihomo" / "config.yaml"
    first = mihomo_config_service.build_mihomo_config(
        {"selective_default": "direct"}, xray_handoff_assignments=[],
    )
    base_path.parent.mkdir(parents=True, exist_ok=True)
    base_path.write_text(mihomo_config_service.dump_mihomo_config(first), encoding="utf-8")

    second = mihomo_config_service.build_mihomo_config(
        {"selective_default": "direct"}, xray_handoff_assignments=[],
    )
    first_meta = first["fwrouter"]
    second_meta = second["fwrouter"]
    changed_metadata = {
        key for key in set(first_meta) | set(second_meta)
        if first_meta.get(key) != second_meta.get(key)
    }

    # The active base contains generated FWRouter listeners, so the second
    # builder pass records their removal. This field is provenance only; all
    # routing and native listener content must remain unchanged.
    assert changed_metadata == {"sanitized_managed_listeners"}
    assert first_meta["sanitized_managed_listeners"] == []
    assert len(second_meta["sanitized_managed_listeners"]) == 5
    assert second_meta["sanitized_legacy_inbound_keys"] == []
    runtime_fields = set(first_meta) - {
        "sanitized_legacy_inbound_keys", "sanitized_managed_listeners",
    }
    assert {key: first_meta[key] for key in runtime_fields} == {
        key: second_meta[key] for key in runtime_fields
    }
    assert first["listeners"] == second["listeners"]
    assert first["rules"] == second["rules"]
    assert first["proxy-groups"] == second["proxy-groups"]

    active_path = get_settings().paths.generated_dir / "mihomo" / "config.yaml"
    candidate_path = tmp_path / "candidate.yaml"
    candidate_path.write_text(mihomo_config_service.dump_mihomo_config(second), encoding="utf-8")
    active_before = active_path.read_bytes()
    assert xray_subscription_service._legacy_mihomo_candidate_matches(
        active_path, candidate_path,
    ) is True
    assert active_path.read_bytes() == active_before

    def candidate_matches(mutator) -> bool:
        altered = json.loads(json.dumps(second))
        mutator(altered)
        candidate_path.write_text(mihomo_config_service.dump_mihomo_config(altered), encoding="utf-8")
        return xray_subscription_service._legacy_mihomo_candidate_matches(active_path, candidate_path)

    assert candidate_matches(lambda value: value["fwrouter"].update({
        "resolved_selective_default": "vpn",
    })) is False
    assert candidate_matches(lambda value: value["rules"].append("MATCH,DIRECT")) is False
    assert candidate_matches(lambda value: value.update({"unknown-runtime-setting": True})) is False
    bad_provenance_matches = candidate_matches(lambda value: value["fwrouter"].update({
        "sanitized_managed_listeners": ["not-present-in-active-config"],
    }))
    import copy
    _, active_sanitization = mihomo_inbounds_service._sanitize_fwrouter_managed_inbounds(
        copy.deepcopy(yaml.safe_load(active_path.read_text(encoding="utf-8"))),
    )
    assert bad_provenance_matches is False, {
        "active_listener_count": len(yaml.safe_load(active_path.read_text(encoding="utf-8")).get("listeners") or []),
        "derived_provenance_count": len(active_sanitization["removed_listener_names"]),
        "candidate_original_provenance_count": len(second_meta["sanitized_managed_listeners"]),
        "candidate_metadata_mutated": True,
    }
    assert candidate_matches(lambda value: value["fwrouter"].update({
        "sanitized_legacy_inbound_keys": "not-a-list",
    })) is False
    assert candidate_matches(lambda value: value["listeners"].append({
        "name": "unexpected-listener", "type": "mixed", "listen": "127.0.0.1", "port": 9,
    })) is False
    assert active_path.read_bytes() == active_before

    candidate_path.write_text(mihomo_config_service.dump_mihomo_config(second), encoding="utf-8")
    candidate_path.unlink()
    candidate_path.symlink_to(active_path)
    assert xray_subscription_service._legacy_mihomo_candidate_matches(
        active_path, candidate_path,
    ) is False
    candidate_path.unlink()
    os.mkfifo(candidate_path)
    assert xray_subscription_service._legacy_mihomo_candidate_matches(
        active_path, candidate_path,
    ) is False
    candidate_path.unlink()

    active_with_legacy_inbound = yaml.safe_load(active_before)
    active_with_legacy_inbound["mixed-port"] = 7890
    active_path.write_text(
        mihomo_config_service.dump_mihomo_config(active_with_legacy_inbound), encoding="utf-8",
    )
    legacy_candidate = mihomo_config_service.build_mihomo_config(
        {"selective_default": "direct"}, xray_handoff_assignments=[],
    )
    candidate_path.write_text(
        mihomo_config_service.dump_mihomo_config(legacy_candidate), encoding="utf-8",
    )
    assert xray_subscription_service._legacy_mihomo_candidate_matches(
        active_path, candidate_path,
    ) is False
    active_path.write_bytes(active_before)

    tests_root = Path(__file__).resolve().parents[2] / "tests"
    monkeypatch.syspath_prepend(str(tests_root))
    from application_acceptance.worker import _safe_mihomo_candidate_diff_summary

    diagnostic_dir = tmp_path / "fwrouter-legacy-admission-diagnostic"
    diagnostic_dir.mkdir()
    diagnostic_candidate = diagnostic_dir / "candidate.yaml"
    diagnostic_candidate.write_text(yaml.safe_dump(second, sort_keys=False), encoding="utf-8")
    monkeypatch.setenv("FWROUTER_ENVIRONMENT", "test")
    monkeypatch.setenv("FWROUTER_APPLICATION_ACCEPTANCE_ROOT", str(tmp_path))
    summary = _safe_mihomo_candidate_diff_summary(
        active_path, diagnostic_candidate, second,
    )
    assert summary is not None
    assert summary["fwrouter_changed_fields"] == ["sanitized_managed_listeners"]
    assert summary["fwrouter_unknown_fields_changed"] is False
    assert "sanitized_managed_listeners" in json.dumps(summary)
    assert second_meta["sanitized_managed_listeners"][0] not in json.dumps(summary)


def test_build_mihomo_config_does_not_resurrect_legacy_managed_inbounds_from_last_good_or_debug(
    monkeypatch,
    tmp_path: Path,
) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()
    monkeypatch.setattr(mihomo_config_service, "_collect_xray_handoff_assignments", lambda: [])

    settings = get_settings()
    last_good = settings.paths.state_dir / "last-good" / "mihomo" / "config.20260101.yaml"
    debug = settings.paths.state_dir / "debug" / "sample" / "mihomo-config.yaml"
    legacy_payload = {
        "allow-lan": False,
        "mixed-port": 6001,
        "tproxy-port": 6002,
        "listeners": [
            {"name": "fwrouter-mixed", "type": "mixed", "listen": "0.0.0.0", "port": 6003, "proxy": "DIRECT"},
            {"name": "fwrouter-tproxy", "type": "tproxy", "listen": "127.0.0.1", "port": 6004, "rule": "legacy"},
        ],
        "proxies": [{"name": "Test VPN", "type": "socks5", "server": "1.1.1.1", "port": 1080}],
        "proxy-groups": [{"name": "vpn-global", "type": "select", "proxies": ["Test VPN", "DIRECT"]}],
    }
    _write_yaml(last_good, legacy_payload)
    _write_yaml(debug, legacy_payload)

    config = mihomo_config_service.build_mihomo_config()

    assert "mixed-port" not in config
    _assert_managed_contour(config)


def test_build_mihomo_config_uses_vpn_fallback_when_requested(monkeypatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()
    monkeypatch.setattr(mihomo_config_service, "_collect_xray_handoff_assignments", lambda: [])

    config = mihomo_config_service.build_mihomo_config({"selective_default": "vpn"})

    _assert_managed_contour(config)
    assert config["rules"][-1] == "MATCH,DIRECT"
    assert config["sub-rules"]["fwrouter-transparent"][-2:] == [
        "MATCH,vpn-global",
        "NETWORK,udp,REJECT",
    ]
    assert config["fwrouter"]["resolved_selective_default"] == "vpn"
    assert config["fwrouter"]["final_match_rule"] == "MATCH,DIRECT"
    assert config["fwrouter"]["transparent_final_match_rule"] == "MATCH,vpn-global"


def test_transparent_fallback_validation_accounts_for_terminal_vpn_udp_guard(
    monkeypatch,
    tmp_path: Path,
) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()
    monkeypatch.setattr(mihomo_config_service, "_collect_xray_handoff_assignments", lambda: [])
    from fwrouter_api.services.mihomo_config_validation import _validate_candidate_structure

    routing = {"desired_mode": "vpn"}
    config = mihomo_config_service.build_mihomo_config(routing)
    structural = _validate_candidate_structure(config, routing=routing)

    assert config["sub-rules"]["fwrouter-transparent"][-2:] == [
        "MATCH,vpn-global",
        "NETWORK,udp,REJECT",
    ]
    assert structural["transparent_final_match_rule"] == "MATCH,vpn-global"
    assert structural["transparent_state_consistency_ok"] is True

    config["sub-rules"]["fwrouter-transparent"][-2] = "MATCH,DIRECT"
    mismatched = _validate_candidate_structure(config, routing=routing)
    assert mismatched["transparent_state_consistency_ok"] is False


def test_build_mihomo_config_forces_ipv4_transparent_listener_mode_over_legacy_ipv6_true(
    monkeypatch,
    tmp_path: Path,
) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()
    monkeypatch.setattr(mihomo_config_service, "_collect_xray_handoff_assignments", lambda: [])

    settings = get_settings()
    _write_yaml(
        settings.paths.generated_dir / "mihomo" / "config.yaml",
        {
            "ipv6": True,
            "allow-lan": True,
            "proxies": [{"name": "Test VPN", "type": "socks5", "server": "1.1.1.1", "port": 1080}],
            "proxy-groups": [{"name": "vpn-global", "type": "select", "proxies": ["Test VPN", "DIRECT"]}],
        },
    )

    config = mihomo_config_service.build_mihomo_config()

    assert config["ipv6"] is False


def test_build_mihomo_config_upgrades_existing_sniffer_for_transparent_pure_ip_recovery(
    monkeypatch,
    tmp_path: Path,
) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()
    monkeypatch.setattr(mihomo_config_service, "_collect_xray_handoff_assignments", lambda: [])

    settings = get_settings()
    _write_yaml(
        settings.paths.generated_dir / "mihomo" / "config.yaml",
        {
            "allow-lan": True,
            "proxies": [{"name": "Test VPN", "type": "socks5", "server": "1.1.1.1", "port": 1080}],
            "proxy-groups": [{"name": "vpn-global", "type": "select", "proxies": ["Test VPN", "DIRECT"]}],
            "sniffer": {
                "enable": False,
                "sniff": {
                    "HTTP": {"ports": [80]},
                    "TLS": {"ports": [443]},
                },
            },
        },
    )

    config = mihomo_config_service.build_mihomo_config()

    assert config["sniffer"]["enable"] is True
    assert config["sniffer"]["force-dns-mapping"] is True
    assert config["sniffer"]["parse-pure-ip"] is True
    assert config["sniffer"]["override-destination"] is True
    assert config["sniffer"]["sniff"]["HTTP"]["ports"] == [80, 8080]
    assert config["sniffer"]["sniff"]["HTTP"]["override-destination"] is True
    assert config["sniffer"]["sniff"]["TLS"]["ports"] == [443, 8443]
    assert config["sniffer"]["sniff"]["TLS"]["override-destination"] is True
    assert config["sniffer"]["sniff"]["QUIC"]["ports"] == [443, 8443]
    assert config["sniffer"]["sniff"]["QUIC"]["override-destination"] is True
    _assert_managed_contour(config)


def test_build_mihomo_config_keeps_transparent_ingress_on_wildcard_bind(
    monkeypatch,
    tmp_path: Path,
) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()
    monkeypatch.setattr(mihomo_config_service, "_collect_xray_handoff_assignments", lambda: [])

    config = mihomo_config_service.build_mihomo_config()

    assert config["bind-address"] == "0.0.0.0"
    assert config["fwrouter"]["transparent_listener_bind"] == "0.0.0.0"


def test_mihomo_config_includes_vpn_auto_servers_even_when_global_list_false(
    monkeypatch,
    tmp_path: Path,
) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()
    monkeypatch.setattr(mihomo_config_service, "_collect_xray_handoff_assignments", lambda: [])
    _seed_runtime_proxy_server(
        "srv-auto-hidden",
        server_name="srv-auto-hidden",
        vpn_auto=True,
        global_list=False,
    )

    config = mihomo_config_service.build_mihomo_config()

    proxy_names = {
        str(proxy.get("name") or "")
        for proxy in config.get("proxies") or []
        if isinstance(proxy, dict)
    }
    groups = {
        str(group.get("name") or ""): list(group.get("proxies") or [])
        for group in config.get("proxy-groups") or []
        if isinstance(group, dict)
    }

    assert "srv-auto-hidden" in proxy_names
    assert "srv-auto-hidden" in groups["vpn-auto"]
    assert "srv-auto-hidden" not in groups["vpn-global"]


def test_mihomo_config_excludes_manual_only_priority_from_vpn_auto(
    monkeypatch,
    tmp_path: Path,
) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()
    monkeypatch.setattr(mihomo_config_service, "_collect_xray_handoff_assignments", lambda: [])
    _seed_runtime_proxy_server(
        "srv-manual-only",
        server_name="srv-manual-only",
        vpn_auto=True,
        global_list=True,
        vpn_auto_priority=-1,
    )

    config = mihomo_config_service.build_mihomo_config()

    groups = {
        str(group.get("name") or ""): list(group.get("proxies") or [])
        for group in config.get("proxy-groups") or []
        if isinstance(group, dict)
    }

    assert "srv-manual-only" not in groups["vpn-auto"]
    assert "srv-manual-only" in groups["vpn-global"]


def test_subscription_profiles_exclude_manual_only_priority_from_vpn_auto(
    monkeypatch,
    tmp_path: Path,
) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()
    _seed_runtime_proxy_server(
        "srv-auto",
        server_name="srv-auto",
        vpn_auto=True,
        global_list=True,
        vpn_auto_priority=0,
    )
    _seed_runtime_proxy_server(
        "srv-manual-only",
        server_name="srv-manual-only",
        vpn_auto=True,
        global_list=True,
        vpn_auto_priority=-1,
    )

    servers = subscription_profiles_service._subscription_servers()
    names = [str(server.get("server_name") or "") for server in servers]

    assert "srv-auto" in names
    assert "srv-manual-only" not in names


def test_xray_vpn_auto_subscription_excludes_manual_only_priority(
    monkeypatch,
    tmp_path: Path,
) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()
    _seed_runtime_proxy_server(
        "srv-auto",
        server_name="srv-auto",
        vpn_auto=True,
        global_list=True,
        vpn_auto_priority=0,
    )
    _seed_runtime_proxy_server(
        "srv-manual-only",
        server_name="srv-manual-only",
        vpn_auto=True,
        global_list=True,
        vpn_auto_priority=-1,
    )
    monkeypatch.setattr(
        xray_subscription_service,
        "_is_xray_supported_server_config",
        lambda raw: (True, "test"),
    )

    servers = xray_subscription_service._vpn_auto_servers_for_xray_subscription()
    names = [str(server.get("server_name") or "") for server in servers]

    assert "srv-auto" in names
    assert "srv-manual-only" not in names


def test_manual_only_custom_proxy_is_explicit_subscription_node(monkeypatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()
    _seed_runtime_proxy_server(
        "custom-https:manual", server_name="Manual Proxy",
        vpn_auto=True, global_list=True, vpn_auto_priority=-1,
    )
    with db_session() as connection:
        connection.execute(
            "INSERT INTO server_custom_https_proxy (server_id, proxy_type, host, port) VALUES (?, 'socks5', 'proxy.example.test', 1080)",
            ("custom-https:manual",),
        )

    profile = subscription_profiles_service._subscription_servers()
    xray = xray_subscription_service._vpn_auto_servers_for_xray_subscription()
    assert any(item["server_id"] == "custom-https:manual" for item in profile)
    assert any(item["server_id"] == "custom-https:manual" for item in xray)
    assert all(item["server_id"] != "custom-https:manual" for item in profile[2:])


def test_build_mihomo_config_renders_effective_domain_and_cidr_rules(monkeypatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()
    monkeypatch.setattr(mihomo_config_service, "_collect_xray_handoff_assignments", lambda: [])

    settings = get_settings()
    _write_effective_rules(
        settings.paths.generated_dir / "rules" / "effective-rules.json",
        {
            "selective_default": "direct",
            "rules": [
                {"action": "VPN", "kind": "domain_suffix", "value": ".instagram.com"},
                {"action": "VPN", "kind": "domain", "value": "graph.facebook.com"},
                {"action": "DIRECT", "kind": "cidr", "value": "192.168.0.0/16"},
            ],
        },
    )

    config = mihomo_config_service.build_mihomo_config()

    assert config["rules"] == [
        "DOMAIN-SUFFIX,instagram.com,vpn-global",
        "AND,((NETWORK,udp),(DOMAIN-SUFFIX,instagram.com)),REJECT",
        "DOMAIN,graph.facebook.com,vpn-global",
        "AND,((NETWORK,udp),(DOMAIN,graph.facebook.com)),REJECT",
        "IP-CIDR,192.168.0.0/16,DIRECT",
        "MATCH,DIRECT",
    ]
    assert config["sub-rules"]["fwrouter-transparent"] == [
        "DOMAIN-SUFFIX,instagram.com,vpn-global",
        "AND,((NETWORK,udp),(DOMAIN-SUFFIX,instagram.com)),REJECT",
        "DOMAIN,graph.facebook.com,vpn-global",
        "AND,((NETWORK,udp),(DOMAIN,graph.facebook.com)),REJECT",
        "IP-CIDR,192.168.0.0/16,DIRECT",
        "MATCH,DIRECT",
    ]
    assert config["fwrouter"]["rendered_rules_count"] == 3


def test_build_mihomo_config_updates_fwrouter_transparent_subrules(
    monkeypatch,
    tmp_path: Path,
) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()
    monkeypatch.setattr(mihomo_config_service, "_collect_xray_handoff_assignments", lambda: [])

    settings = get_settings()
    _write_effective_rules(
        settings.paths.generated_dir / "rules" / "effective-rules.json",
        {
            "selective_default": "direct",
            "rules": [
                {"action": "VPN", "kind": "domain_suffix", "value": ".instagram.com"},
            ],
        },
    )
    _write_yaml(
        settings.paths.generated_dir / "mihomo" / "config.yaml",
        {
            "proxies": [{"name": "Test VPN", "type": "socks5", "server": "1.1.1.1", "port": 1080}],
            "proxy-groups": [{"name": "vpn-global", "type": "select", "proxies": ["Test VPN", "DIRECT"]}],
            "sub-rules": {"fwrouter-transparent": ["MATCH,DIRECT"]},
        },
    )

    config = mihomo_config_service.build_mihomo_config()

    assert config["rules"] == [
        "DOMAIN-SUFFIX,instagram.com,vpn-global",
        "AND,((NETWORK,udp),(DOMAIN-SUFFIX,instagram.com)),REJECT",
        "MATCH,DIRECT",
    ]
    assert config["sub-rules"]["fwrouter-transparent"] == [
        "DOMAIN-SUFFIX,instagram.com,vpn-global",
        "AND,((NETWORK,udp),(DOMAIN-SUFFIX,instagram.com)),REJECT",
        "MATCH,DIRECT",
    ]


def test_build_mihomo_config_rebuilds_rules_without_represerving_old_effective_entries(
    monkeypatch,
    tmp_path: Path,
) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()
    monkeypatch.setattr(mihomo_config_service, "_collect_xray_handoff_assignments", lambda: [])

    settings = get_settings()
    _write_effective_rules(
        settings.paths.generated_dir / "rules" / "effective-rules.json",
        {
            "selective_default": "direct",
            "rules": [
                {"action": "VPN", "kind": "domain_suffix", "value": ".instagram.com"},
            ],
        },
    )
    _write_yaml(
        settings.paths.generated_dir / "mihomo" / "config.yaml",
        {
            "proxies": [{"name": "Test VPN", "type": "socks5", "server": "1.1.1.1", "port": 1080}],
            "proxy-groups": [{"name": "vpn-global", "type": "select", "proxies": ["Test VPN", "DIRECT"]}],
            "rules": [
                "DOMAIN-SUFFIX,legacy.example,vpn-global",
                "DOMAIN-SUFFIX,instagram.com,vpn-global",
                "MATCH,DIRECT",
            ],
        },
    )

    config = mihomo_config_service.build_mihomo_config()

    assert config["rules"] == [
        "DOMAIN-SUFFIX,instagram.com,vpn-global",
        "AND,((NETWORK,udp),(DOMAIN-SUFFIX,instagram.com)),REJECT",
        "MATCH,DIRECT",
    ]
    assert "DOMAIN-SUFFIX,legacy.example,vpn-global" not in config["rules"]
    assert config["sub-rules"]["fwrouter-transparent"] == [
        "DOMAIN-SUFFIX,instagram.com,vpn-global",
        "AND,((NETWORK,udp),(DOMAIN-SUFFIX,instagram.com)),REJECT",
        "MATCH,DIRECT",
    ]


def test_build_mihomo_config_skips_oversized_base_config(monkeypatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()
    monkeypatch.setattr(mihomo_config_service, "_collect_xray_handoff_assignments", lambda: [])

    settings = get_settings()
    _write_effective_rules(
        settings.paths.generated_dir / "rules" / "effective-rules.json",
        {
            "selective_default": "direct",
            "rules": [
                {"action": "VPN", "kind": "domain_suffix", "value": ".instagram.com"},
            ],
        },
    )
    oversized = settings.paths.generated_dir / "mihomo" / "config.yaml"
    oversized.parent.mkdir(parents=True, exist_ok=True)
    oversized.write_text("rules:\n" + ("- DOMAIN-SUFFIX,legacy.example,vpn-global\n" * 300000), encoding="utf-8")

    config = mihomo_config_service.build_mihomo_config()

    assert config["rules"] == [
        "DOMAIN-SUFFIX,instagram.com,vpn-global",
        "AND,((NETWORK,udp),(DOMAIN-SUFFIX,instagram.com)),REJECT",
        "MATCH,DIRECT",
    ]
    assert config["sub-rules"]["fwrouter-transparent"] == [
        "DOMAIN-SUFFIX,instagram.com,vpn-global",
        "AND,((NETWORK,udp),(DOMAIN-SUFFIX,instagram.com)),REJECT",
        "MATCH,DIRECT",
    ]
    assert "DOMAIN-SUFFIX,legacy.example,vpn-global" not in config["rules"]
    assert config["external-controller"] == mihomo_config_service.MIHOMO_CONTROLLER_ADDRESS


def test_build_mihomo_config_uses_direct_transparent_subrule_fallback_when_selective_default_is_direct(
    monkeypatch,
    tmp_path: Path,
) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()
    monkeypatch.setattr(mihomo_config_service, "_collect_xray_handoff_assignments", lambda: [])

    config = mihomo_config_service.build_mihomo_config({"selective_default": "direct"})

    transparent_rules = config["sub-rules"]["fwrouter-transparent"]
    assert transparent_rules
    assert transparent_rules[-1] == "MATCH,DIRECT"
    assert config["rules"][-1] == "MATCH,DIRECT"


def _assert_reconcile_mihomo_runtime_skips_restart_for_unchanged_config(
    monkeypatch,
    tmp_path: Path,
    *,
    format_only_candidate: bool,
    native_proof_ok: bool,
    verification_callback_ok: bool | None = None,
    fixed_restart_wiring: bool = False,
) -> dict:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()
    monkeypatch.setattr("fwrouter_api.services.mihomo_reconcile._mihomo_incarnation", lambda: "pytest-runtime")
    monkeypatch.setattr(mihomo_reconcile_fingerprint_service, "_current_runtime_incarnation", lambda: "pytest-runtime")
    native_proof_calls: list[bool] = []
    monkeypatch.setattr("fwrouter_api.services.mihomo_reconcile._native_udp_policy_proof", lambda: native_proof_calls.append(True) or {
        "ok": native_proof_ok, "native_top_level_rules_match": True,
        "selector_membership_matches": True, "guarded_rules_match": True,
    })
    monkeypatch.setattr(mihomo_config_service, "_collect_xray_handoff_assignments", lambda: [])

    candidate_config = mihomo_config_service.build_mihomo_config({"selective_default": "direct"})
    base_path = Path(mihomo_config_service._resolved_base_config_path())
    candidate_path = Path(mihomo_config_service._resolved_candidate_config_path())
    _write_yaml(base_path, {"rules": ["MATCH,REJECT"]} if fixed_restart_wiring else candidate_config)
    restart_calls: list[str] = []
    materialization_callback = lambda **kwargs: {"ok": False}


    def _fake_restart_mihomo_container(*, action: str = "restart", heartbeat=None, **kwargs):
        if fixed_restart_wiring:
            from fwrouter_api.adapters.xray_common import xray_writer_guard_is_held
            assert xray_writer_guard_is_held()
            assert kwargs["selection_fenced"] is True
            assert type(kwargs["expected_selection_revision"]) is int
            assert kwargs["generation_verification_callback"] is materialization_callback
            assert kwargs["operation_id"]
            raise RuntimeError("FIXED_RESTART_WIRING_REACHED")
        restart_calls.append(action)
        return {"ok": True, "action": action}

    monkeypatch.setattr(mihomo_config_service, "restart_mihomo_container", _fake_restart_mihomo_container)
    monkeypatch.setattr(mihomo_config_service, "get_mihomo_config_status", lambda **kwargs: {
        "base_config": candidate_config,
        "candidate_config": candidate_config,
        "base_path": str(base_path),
        "candidate_path": str(candidate_path),
        "base_exists": True,
        "candidate_exists": True,
        "base_updated_at": "2026-06-08T00:00:00+00:00",
        "candidate_updated_at": "2026-06-08T00:00:00+00:00",
        "base_rules_count": len(candidate_config.get("rules") or []),
        "candidate_rules_count": len(candidate_config.get("rules") or []),
    })
    def _fake_write_mihomo_candidate_config(routing=None):
        candidate_path.parent.mkdir(parents=True, exist_ok=True)
        candidate_text = (
            yaml.safe_dump(candidate_config, sort_keys=True, indent=4)
            if format_only_candidate
            else yaml.safe_dump(candidate_config, sort_keys=False)
        )
        candidate_path.write_text(candidate_text, encoding="utf-8")
        return {
            "candidate_path": str(candidate_path),
            "rules": candidate_config["rules"],
            "handoff_assignments": [],
            "resolved_selective_default": "direct",
            "final_match_rule": "MATCH,DIRECT",
            "transparent_final_match_rule": "MATCH,DIRECT",
            "config": candidate_config,
        }

    monkeypatch.setattr(
        mihomo_config_service,
        "write_mihomo_candidate_config",
        _fake_write_mihomo_candidate_config,
    )
    monkeypatch.setattr(mihomo_config_service, "validate_mihomo_candidate_config", lambda routing=None: {
        "ok": True,
        "resolved_selective_default": "direct",
        "final_match_rule": "MATCH,DIRECT",
        "expected_final_match_rule": "MATCH,DIRECT",
        "transparent_final_match_rule": "MATCH,DIRECT",
        "expected_transparent_final_match_rule": "MATCH,DIRECT",
        "state_consistency_ok": True,
        "transparent_state_consistency_ok": True,
    })

    with db_session() as connection:
        revision_before = read_selection_revision(connection)
    verification_callback = None
    if verification_callback_ok is not None:
        def verification_callback(*, operation_id: str, expected_selection_revision: int) -> dict:
            return {
                "ok": verification_callback_ok,
                "operation_id": operation_id,
                "selection_revision": expected_selection_revision,
            }

    if fixed_restart_wiring:
        monkeypatch.setattr(mihomo_reconcile_service, "promote_mihomo_candidate_config", lambda **kwargs: {"ok": True})
        with pytest.raises(RuntimeError, match="FIXED_RESTART_WIRING_REACHED"):
            mihomo_config_service.reconcile_mihomo_runtime(
                {"selective_default": "direct", "server_mode": "fixed", "desired_fixed_server_id": "synthetic-fixed"},
                verification_callback=verification_callback,
                generation_materialization_callback=materialization_callback,
            )
        return {}
    result = mihomo_config_service.reconcile_mihomo_runtime(
        {"selective_default": "direct"},
        verification_callback=verification_callback,
    )

    if verification_callback_ok is False:
        assert result["ok"] is False
        assert result["stage"] == "verification"
        assert result.get("reentry_generation_fence") is None
    elif native_proof_ok:
        assert result["ok"] is True
        assert result["reconcile_action"] == "none"
        assert result["reconcile_reason"] == "unchanged_config"
        assert "config" not in (result["candidate"] or {})
        assert result["candidate"]["rules_count"] == len(candidate_config["rules"])
        assert "base_config" not in (result["config"] or {})
        assert "candidate_config" not in (result["config"] or {})
        if verification_callback_ok is True:
            fence = result.get("reentry_generation_fence")
            assert isinstance(fence, dict)
            assert fence["operation_id"] == result["operation_id"]
            assert fence["before_revision"] == revision_before
            assert fence["selection_revision"] == revision_before
            assert fence["unchanged_config"] is True
            assert fence["native_proof_verified"] is True
            assert fence["publication_verified"] is True
    else:
        assert result["ok"] is False
        assert result["stage"] == "native_udp_policy_readback"
        assert result["reconcile_reason"] == "native_policy_or_fence_unconfirmed"
    assert restart_calls == []
    assert bool(native_proof_calls) == (verification_callback_ok is not False)
    return result


def test_reconcile_mihomo_runtime_skips_restart_for_unchanged_config(
    monkeypatch,
    tmp_path: Path,
) -> None:
    # Preserve the historical exact-byte scenario and node ID used by CI.
    _assert_reconcile_mihomo_runtime_skips_restart_for_unchanged_config(
        monkeypatch,
        tmp_path,
        format_only_candidate=False,
        native_proof_ok=True,
    )


def test_unchanged_reconcile_returns_only_same_revision_owner_receipt(monkeypatch, tmp_path: Path) -> None:
    result = _assert_reconcile_mihomo_runtime_skips_restart_for_unchanged_config(
        monkeypatch,
        tmp_path,
        format_only_candidate=False,
        native_proof_ok=True,
        verification_callback_ok=True,
    )
    assert result["reentry_generation_fence"]["selection_revision"] == result["reentry_generation_fence"]["before_revision"]


def test_unchanged_reconcile_refuses_receipt_when_owner_readback_fails(monkeypatch, tmp_path: Path) -> None:
    result = _assert_reconcile_mihomo_runtime_skips_restart_for_unchanged_config(
        monkeypatch,
        tmp_path,
        format_only_candidate=False,
        native_proof_ok=True,
        verification_callback_ok=False,
    )
    assert result.get("reentry_generation_fence") is None


@pytest.mark.parametrize("format_only_candidate", [False, True])
@pytest.mark.parametrize("native_proof_ok", [True, False])
def test_reconcile_mihomo_runtime_skips_restart_for_semantically_unchanged_config(
    monkeypatch,
    tmp_path: Path,
    format_only_candidate: bool,
    native_proof_ok: bool,
) -> None:
    _assert_reconcile_mihomo_runtime_skips_restart_for_unchanged_config(
        monkeypatch,
        tmp_path,
        format_only_candidate=format_only_candidate,
        native_proof_ok=native_proof_ok,
    )


def test_semantic_config_comparison_rejects_ambiguous_missing_and_oversized_yaml(
    tmp_path: Path,
) -> None:
    active = tmp_path / "active.yaml"
    candidate = tmp_path / "candidate.yaml"
    active.write_text("mode: rule\nmode: global\n", encoding="utf-8")
    candidate.write_text("mode: global\n", encoding="utf-8")
    assert mihomo_reconcile_service._semantic_config_files_equal(str(active), str(candidate)) is False

    active.write_text("mode: [unterminated\n", encoding="utf-8")
    assert mihomo_reconcile_service._semantic_config_files_equal(str(active), str(candidate)) is False

    active.write_text("mode: global\n", encoding="utf-8")
    missing = tmp_path / "missing.yaml"
    assert mihomo_reconcile_service._semantic_config_files_equal(str(active), str(missing)) is False

    active.write_text(
        "".join(f"key-{index}: value\n" for index in range(50_050)),
        encoding="utf-8",
    )
    assert mihomo_reconcile_service._semantic_config_files_equal(str(active), str(candidate)) is False

    with active.open("wb") as stream:
        stream.truncate(20 * 1024 * 1024)
    assert mihomo_reconcile_service._semantic_config_files_equal(str(active), str(candidate)) is False


@pytest.mark.parametrize(
    "aliased_yaml",
    [
        "shared: &value repeated\nfirst: *value\nsecond: *value\n",
        "shared: &value [one, two]\nfirst: *value\nsecond: *value\n",
        "cycle: &value [*value]\n",
    ],
    ids=["scalar-alias", "sequence-alias", "cycle"],
)
def test_semantic_config_comparison_rejects_yaml_aliases_before_construction(
    monkeypatch,
    tmp_path: Path,
    aliased_yaml: str,
) -> None:
    active = tmp_path / "active.yaml"
    candidate = tmp_path / "candidate.yaml"
    active.write_text(aliased_yaml, encoding="utf-8")
    candidate.write_text("plain: repeated\n", encoding="utf-8")

    def _must_not_run(*args, **kwargs):
        pytest.fail("alias-rejected YAML must not be constructed or compared")

    monkeypatch.setattr(mihomo_reconcile_service.yaml, "load", _must_not_run)
    monkeypatch.setattr(mihomo_config_service, "_configs_equal", _must_not_run)

    assert mihomo_reconcile_service._semantic_config_files_equal(str(active), str(candidate)) is False


def test_semantic_config_comparison_allows_independent_equal_scalars(tmp_path: Path) -> None:
    active = tmp_path / "active.yaml"
    candidate = tmp_path / "candidate.yaml"
    active.write_text("first: repeated\nsecond: repeated\n", encoding="utf-8")
    candidate.write_text("first: repeated\nsecond: repeated\n", encoding="utf-8")

    assert mihomo_reconcile_service._semantic_config_files_equal(str(active), str(candidate)) is True


def test_reconcile_mihomo_runtime_skips_candidate_generation_when_inputs_unchanged(
    monkeypatch,
    tmp_path: Path,
) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()
    monkeypatch.setattr("fwrouter_api.services.mihomo_reconcile._mihomo_incarnation", lambda: "pytest-runtime")
    monkeypatch.setattr(mihomo_reconcile_fingerprint_service, "_current_runtime_incarnation", lambda: "pytest-runtime")
    monkeypatch.setattr("fwrouter_api.services.mihomo_reconcile._native_udp_policy_proof", lambda: {
        "ok": True, "native_top_level_rules_match": True,
        "selector_membership_matches": True, "guarded_rules_match": True,
    })
    settings = get_settings()
    base_path = settings.paths.generated_dir / "mihomo" / "config.yaml"
    base_path.parent.mkdir(parents=True, exist_ok=True)
    base_path.write_text("rules:\n- MATCH,DIRECT\n", encoding="utf-8")

    fingerprint = current_mihomo_input_fingerprint({"selective_default": "direct"})
    write_mihomo_reconcile_fingerprint_state(
        fingerprint=fingerprint,
        result={"reconcile_action": "none", "reconcile_reason": "unchanged_config"},
        native_proof={
            "native_top_level_rules_match": True,
            "selector_membership_matches": True,
            "guarded_rules_match": True,
        },
        expected_active_hash=mihomo_reconcile_fingerprint_service.active_config_hash(),
        expected_runtime_incarnation="pytest-runtime",
    )

    monkeypatch.setattr(
        mihomo_config_service,
        "write_mihomo_candidate_config",
        lambda routing=None: (_ for _ in ()).throw(AssertionError("candidate generation should be skipped")),
    )
    monkeypatch.setattr(
        mihomo_config_service,
        "validate_mihomo_candidate_config",
        lambda routing=None: (_ for _ in ()).throw(AssertionError("candidate validation should be skipped")),
    )
    monkeypatch.setattr(
        mihomo_config_service,
        "restart_mihomo_container",
        lambda **kwargs: (_ for _ in ()).throw(AssertionError("restart should be skipped")),
    )

    result = mihomo_config_service.reconcile_mihomo_runtime({"selective_default": "direct"})

    assert result["ok"] is True
    assert result["candidate"]["skipped"] is True
    assert result["config_validation"]["skipped"] is True
    assert result["reconcile_action"] == "none"
    assert result["reconcile_reason"] == "input_fingerprint_unchanged"


def test_mihomo_config_status_uses_summary_by_default(monkeypatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()

    settings = get_settings()
    base_path = settings.paths.generated_dir / "mihomo" / "config.yaml"
    candidate_path = settings.paths.generated_dir / "mihomo" / "config.next.yaml"
    _write_yaml(base_path, {"rules": ["MATCH,DIRECT", "DOMAIN-SUFFIX,example.com,DIRECT"]})
    _write_yaml(candidate_path, {"rules": ["MATCH,DIRECT"]})

    def _fail_safe_load(path: str):
        raise AssertionError(f"unexpected full YAML load for {path}")

    monkeypatch.setattr(mihomo_config_service, "_safe_load_yaml", _fail_safe_load)

    status = mihomo_config_service.get_mihomo_config_status()

    assert status["base_exists"] is True
    assert status["candidate_exists"] is True
    assert status["base_rules_count"] == 2
    assert status["candidate_rules_count"] == 1
    assert "base_config" not in status
    assert "candidate_config" not in status


def test_mihomo_config_status_can_include_full_payload(monkeypatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()

    settings = get_settings()
    base_path = settings.paths.generated_dir / "mihomo" / "config.yaml"
    candidate_path = settings.paths.generated_dir / "mihomo" / "config.next.yaml"
    _write_yaml(base_path, {"rules": ["MATCH,DIRECT"]})
    _write_yaml(candidate_path, {"rules": ["MATCH,DIRECT", "MATCH,REJECT"]})

    status = mihomo_config_service.get_mihomo_config_status(include_config=True)

    assert status["base_rules_count"] == 1
    assert status["candidate_rules_count"] == 2
    assert status["base_config"]["rules"] == ["MATCH,DIRECT"]
    assert status["candidate_config"]["rules"] == ["MATCH,DIRECT", "MATCH,REJECT"]


def test_validate_mihomo_candidate_config_rejects_empty_proxyset_with_runtime_inventory(
    monkeypatch,
    tmp_path: Path,
) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()
    candidate_config = _base_candidate()
    candidate_config["proxies"] = []
    _write_yaml(Path(mihomo_config_service._resolved_candidate_config_path()), candidate_config)
    monkeypatch.setattr(
        mihomo_config_service,
        "resolve_mihomo_runtime_proxy_rows",
        lambda **kwargs: [
            {
                "raw": {
                    "name": "Test VPN",
                    "type": "socks5",
                    "server": "1.1.1.1",
                    "port": 1080,
                }
            }
        ],
    )

    result = mihomo_config_service.validate_mihomo_candidate_config({"desired_mode": "vpn"})

    assert result["ok"] is False
    assert result["error_code"] == "MIHOMO_PROXYSET_EMPTY"
    assert result["runtime_proxy_inventory_count"] == 1
    assert result["candidate_proxies_count"] == 0


def test_validate_mihomo_candidate_config_rejects_missing_transparent_listener_target(
    monkeypatch,
    tmp_path: Path,
) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()
    candidate_config = _base_candidate()
    candidate_config["listeners"] = [
        listener
        for listener in candidate_config["listeners"]
        if listener["name"] != "fwrouter-redir"
    ]
    _write_yaml(Path(mihomo_config_service._resolved_candidate_config_path()), candidate_config)
    monkeypatch.setattr(mihomo_config_service, "_validate_candidate_with_binary", lambda path: {
        "available": False,
        "binary": None,
        "ok": True,
        "returncode": 0,
        "stdout_tail": "",
        "stderr_tail": "",
    })

    result = mihomo_config_service.validate_mihomo_candidate_config({"desired_mode": "vpn"})

    assert result["ok"] is False
    assert result["error_code"] == "MIHOMO_TRANSPARENT_LISTENER_CONFLICT"


def test_validate_mihomo_candidate_config_rejects_missing_xray_handoff_target(
    monkeypatch,
    tmp_path: Path,
) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()
    candidate_config = _base_candidate()
    candidate_config["listeners"].append(
        {
            "name": "fwrouter-xray-egress-deadbeef",
            "type": "mixed",
            "listen": "172.18.0.1",
            "port": 55000,
            "udp": True,
            "proxy": "Missing Proxy",
        }
    )
    _write_yaml(Path(mihomo_config_service._resolved_candidate_config_path()), candidate_config)
    monkeypatch.setattr(mihomo_config_service, "_validate_candidate_with_binary", lambda path: {
        "available": False,
        "binary": None,
        "ok": True,
        "returncode": 0,
        "stdout_tail": "",
        "stderr_tail": "",
    })

    result = mihomo_config_service.validate_mihomo_candidate_config()

    assert result["ok"] is False
    assert result["error_code"] == "MIHOMO_XRAY_HANDOFF_TARGET_MISSING"


def test_validate_mihomo_candidate_config_rejects_loopback_bound_transparent_listener(
    monkeypatch,
    tmp_path: Path,
) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()
    candidate_config = _base_candidate()
    for listener in candidate_config["listeners"]:
        if listener["name"] in {"fwrouter-redir", "fwrouter-tproxy"}:
            listener["listen"] = "127.0.0.1"
    _write_yaml(Path(mihomo_config_service._resolved_candidate_config_path()), candidate_config)
    monkeypatch.setattr(mihomo_config_service, "_validate_candidate_with_binary", lambda path: {
        "available": False,
        "binary": None,
        "ok": True,
        "returncode": 0,
        "stdout_tail": "",
        "stderr_tail": "",
    })

    result = mihomo_config_service.validate_mihomo_candidate_config({"desired_mode": "vpn"})

    assert result["ok"] is False
    assert result["error_code"] == "MIHOMO_TRANSPARENT_LISTENER_BIND_INVALID"


def test_validate_mihomo_candidate_config_accepts_split_redir_tproxy_contract(
    monkeypatch,
    tmp_path: Path,
) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()
    candidate_config = _base_candidate()
    _write_yaml(Path(mihomo_config_service._resolved_candidate_config_path()), candidate_config)
    monkeypatch.setattr(mihomo_config_service, "_validate_candidate_with_binary", lambda path: {
        "available": False,
        "binary": None,
        "ok": True,
        "returncode": 0,
        "stdout_tail": "",
        "stderr_tail": "",
    })

    result = mihomo_config_service.validate_mihomo_candidate_config({"desired_mode": "vpn"})

    assert result["ok"] is True
    assert result["error_code"] is None
    assert result["transparent_redir_port"] == 5202
    assert result["transparent_listener_port"] == 5203
    assert result["transparent_inbound_rule_ok"] is True


def test_reconcile_mihomo_selective_default_fast_requires_full_compile_when_udp_guard_sequence_changes(
    monkeypatch,
    tmp_path: Path,
) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()
    _enable_vpn_module()
    base_path = Path(mihomo_config_service._resolved_base_config_path())
    base_path.parent.mkdir(parents=True, exist_ok=True)
    base_path.write_text(
        "\n".join(
            [
                "rules:",
                "  fwrouter-transparent:",
                "  - DOMAIN-SUFFIX,example.test,vpn-global",
                "  - MATCH,DIRECT",
                "  fwrouter-full-vpn:",
                "  - MATCH,vpn-global",
                "fwrouter:",
                "  resolved_selective_default: direct",
                "  final_match_rule: MATCH,DIRECT",
                "  transparent_final_match_rule: MATCH,DIRECT",
                "  rendered_rules_count: 2",
                "",
            ]
        ),
        encoding="utf-8",
    )
    candidate_path = Path(mihomo_config_service._resolved_candidate_config_path())
    base_before = base_path.read_bytes()
    candidate_before = candidate_path.read_bytes() if candidate_path.exists() else None
    with db_session() as connection:
        revision_before = read_selection_revision(connection)
    monkeypatch.setattr(
        mihomo_config_service,
        "restart_mihomo_container",
        lambda **kwargs: (_ for _ in ()).throw(AssertionError("deferred fast path must not restart")),
    )

    result = mihomo_config_service.reconcile_mihomo_selective_default_fast(
        {"desired_mode": "selective", "applied_mode": "selective", "selective_default": "vpn"},
        job_id="pytest-fast-fallback",
    )

    assert result["ok"] is False
    assert result["fast_path"] is True
    assert result["fallback_required"] is True
    assert result["reconcile_action"] == "none"
    assert result["reconcile_reason"] == "verified_publication_requires_full_reconcile"
    assert base_path.read_bytes() == base_before
    assert (candidate_path.read_bytes() if candidate_path.exists() else None) == candidate_before
    with db_session() as connection:
        assert read_selection_revision(connection) == revision_before


def test_reconcile_rollback_classifies_actual_direct_and_mandatory_vpn_configs(
    monkeypatch, tmp_path: Path,
) -> None:
    from fwrouter_api.services.mihomo_reconcile import (
        _capture_mihomo_reconcile_checkpoint,
        _checkpoint_rollback_policy_allowed,
    )

    monkeypatch.setattr(
        mihomo_config_service, "_resolved_last_good_mihomo_dir", lambda: tmp_path / "last-good",
    )
    base = tmp_path / "config.yaml"
    candidate = tmp_path / "candidate.yaml"
    candidate.write_text("rules: []\n", encoding="utf-8")
    # Direct mode does not use the dormant full-VPN listener fallback.
    base.write_text(yaml.safe_dump({
        "rules": ["MATCH,DIRECT"],
        "proxy-groups": [
            {"name": "vpn-auto", "proxies": ["DIRECT"]},
            {"name": "vpn-global", "proxies": ["vpn-auto", "DIRECT"]},
        ],
        "sub-rules": {"fwrouter-transparent": ["MATCH,DIRECT"], "fwrouter-full-vpn": ["MATCH,vpn-global"]},
    }), encoding="utf-8")
    direct = _capture_mihomo_reconcile_checkpoint(str(base), str(candidate), routing={"applied_mode": "direct"})
    assert direct["before_mandatory_vpn_policy_required"] is False
    assert _checkpoint_rollback_policy_allowed(direct) is True

    # A live mandatory VPN rule makes the same legacy selector/rule contract unsafe to restore.
    base.write_text(yaml.safe_dump({
        "rules": ["DOMAIN,example.test,vpn-global", "MATCH,DIRECT"],
        "proxy-groups": [
            {"name": "vpn-auto", "proxies": ["server-a", "DIRECT"]},
            {"name": "vpn-global", "proxies": ["vpn-auto", "DIRECT"]},
        ],
        "sub-rules": {"fwrouter-transparent": ["MATCH,DIRECT"], "fwrouter-full-vpn": ["MATCH,vpn-global"]},
    }), encoding="utf-8")
    mandatory = _capture_mihomo_reconcile_checkpoint(str(base), str(candidate), routing={"applied_mode": "direct"})
    assert mandatory["before_mandatory_vpn_policy_required"] is True
    assert mandatory["before_udp_policy_safe"] is False
    assert _checkpoint_rollback_policy_allowed(mandatory) is False

    base.write_text(yaml.safe_dump({
        "rules": ["DOMAIN,example.test,vpn-global", "AND,((NETWORK,udp),(DOMAIN,example.test)),REJECT", "MATCH,DIRECT"],
        "proxy-groups": [
            {"name": "vpn-auto", "proxies": ["server-a", "REJECT"]},
            {"name": "vpn-global", "proxies": ["vpn-auto", "REJECT"]},
        ],
        "sub-rules": {
            "fwrouter-transparent": ["MATCH,DIRECT"],
            "fwrouter-full-vpn": ["MATCH,vpn-global", "NETWORK,udp,REJECT"],
        },
    }), encoding="utf-8")
    safe = _capture_mihomo_reconcile_checkpoint(str(base), str(candidate), routing={"applied_mode": "vpn"})
    assert safe["before_mandatory_vpn_policy_required"] is True
    assert safe["before_udp_policy_safe"] is True
    assert _checkpoint_rollback_policy_allowed(safe) is True


def test_mihomo_udp_policy_receipt_is_bound_to_config_hash_runtime_and_native_proof(
    monkeypatch, tmp_path: Path,
) -> None:
    from fwrouter_api.services import mihomo_reconcile_fingerprint as fingerprint_service

    get_live_probe_cache(
        "dataplane_global.mihomo_health",
        ttl_seconds=60,
        loader=lambda: {"policy_installed": False},
    )
    get_live_probe_cache(
        "dataplane_status.runtime_enforcement",
        ttl_seconds=60,
        loader=lambda: {"enforcement_level": "global_direct_only"},
    )
    receipt_path = tmp_path / "reconcile-state.json"
    monkeypatch.setattr(fingerprint_service, "_state_path", lambda: receipt_path)
    monkeypatch.setattr(fingerprint_service, "active_config_hash", lambda: "active-hash-a")
    monkeypatch.setattr(fingerprint_service, "_current_runtime_incarnation", lambda: "container-a|started-a")
    written = fingerprint_service.write_mihomo_reconcile_fingerprint_state(
        fingerprint={"hash": "inputs-a"},
        result={"reconcile_reason": "test"},
        native_proof={
            "native_top_level_rules_match": True,
            "selector_membership_matches": True,
            "guarded_rules_match": True,
        },
        expected_active_hash="active-hash-a",
        expected_runtime_incarnation="container-a|started-a",
    )
    assert written is True
    assert peek_live_probe_cache("dataplane_global.mihomo_health") is None
    assert peek_live_probe_cache("dataplane_status.runtime_enforcement") is None
    assert fingerprint_service.mihomo_udp_policy_receipt_matches() is True
    monkeypatch.setattr(fingerprint_service, "active_config_hash", lambda: "active-hash-b")
    assert fingerprint_service.mihomo_udp_policy_receipt_matches() is False
    monkeypatch.setattr(fingerprint_service, "active_config_hash", lambda: "active-hash-a")
    monkeypatch.setattr(fingerprint_service, "_current_runtime_incarnation", lambda: "container-b|started-b")
    assert fingerprint_service.mihomo_udp_policy_receipt_matches() is False


def test_xray_handoff_candidate_retains_last_applied_listener(monkeypatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()
    monkeypatch.setattr(xray_service, "collect_xray_runtime_bindings", lambda: [])
    monkeypatch.setattr(
        xray_runtime_state_service,
        "_load_xray_bindings_state",
        lambda: {
            "bindings": [{
                "status": "applied",
                "client_email": "sub-old@fwrouter.local",
                "selected_server_id": "server-old",
                "handoff_proxy_name": "Old Runtime Name [deadbeef]",
                "handoff": {
                    "listener_name": "fwrouter-xray-egress-deadbeef",
                    "listen": "172.18.0.1",
                    "port": 56123,
                    "outbound_tag": "fwrouter-egress-deadbeef",
                },
            }]
        },
    )

    assignments = mihomo_inbounds_service._collect_xray_handoff_assignments()

    assert assignments == [{
        "selected_server_id": "server-old",
        "listener_name": "fwrouter-xray-egress-deadbeef",
        "listen": "172.18.0.1",
        "port": 56123,
        "tag": "fwrouter-egress-deadbeef",
        "proxy": "Old Runtime Name [deadbeef]",
        "client_emails": ["sub-old@fwrouter.local"],
    }]


def test_xray_handoff_candidate_retains_required_last_good_proxy(monkeypatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()
    monkeypatch.setattr(
        mihomo_proxies_service,
        "resolve_mihomo_runtime_proxy_rows",
        lambda **_kwargs: [],
    )
    old_proxy = {
        "name": "Old Runtime Name [deadbeef]",
        "type": "socks5",
        "server": "203.0.113.42",
        "port": 1080,
    }

    proxies = mihomo_proxies_service._merge_runtime_proxies(
        {"proxies": [old_proxy]},
        required_last_good_names={"Old Runtime Name [deadbeef]"},
    )

    assert proxies == [old_proxy]


def test_structured_profile_materializes_members_and_keeps_logical_fallback_group(monkeypatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()
    logical_id = "sub:logical-profile"
    logical_name = "Logical profile [12345678]"
    member_a = "sub:member-a"
    member_b = "sub:member-b"
    raw = {
        "name": logical_name,
        "_fwrouter_runtime_name": logical_name,
        "_fwrouter_topology": {
            "kind": "logical_profile",
            "endpoints": [
                {
                    "identity": member_a,
                    "runtime": {
                        "name": "upstream-a",
                        "type": "vless",
                        "server": "198.51.100.10",
                        "port": 443,
                        "uuid": "uuid-a",
                        "network": "tcp",
                        "tls": True,
                        "security": "reality",
                    },
                },
                {
                    "identity": member_b,
                    "runtime": {
                        "name": "upstream-b",
                        "type": "vless",
                        "server": "198.51.100.11",
                        "port": 443,
                        "uuid": "uuid-b",
                        "network": "grpc",
                        "tls": True,
                        "security": "reality",
                    },
                },
            ],
        },
    }
    with db_session() as connection:
        connection.execute(
            "INSERT INTO servers (server_id, server_name, provider_name, raw_json, inventory_state) VALUES (?, ?, 'subscription', ?, 'active')",
            (logical_id, "Logical profile", json.dumps(raw)),
        )
        connection.execute(
            "INSERT INTO server_preferences (server_id, vpn_auto, global_list) VALUES (?, 1, 1)",
            (logical_id,),
        )

    proxies = mihomo_proxies_service._merge_runtime_proxies({"proxies": []})
    groups = mihomo_proxies_service._ensure_selector_groups({"proxies": proxies})
    by_name = {group["name"]: group for group in groups}

    assert [proxy["name"] for proxy in proxies] == [
        f"{logical_name} :: member-a",
        f"{logical_name} :: member-b",
    ]
    assert by_name[logical_name]["type"] == "fallback"
    assert by_name[logical_name]["proxies"] == [proxy["name"] for proxy in proxies]
    assert logical_name in by_name["vpn-auto"]["proxies"]
    assert all(member not in by_name["vpn-auto"]["proxies"] for member in by_name[logical_name]["proxies"])


def test_mihomo_rebuild_prunes_only_exact_stale_owned_profile_group(
    monkeypatch, tmp_path: Path,
) -> None:
    """A refresh may remove an old generated profile group, but not user groups.

    The inactive DB row is the ownership witness. The old generated fallback
    shape is matched exactly; an edited group with the same name is retained so
    that native Mihomo validation can reject a dangling reference safely.
    """
    _configure_env(monkeypatch, tmp_path)
    initialize_database()
    logical_id = "sub:stale-logical-profile"
    logical_name = "Stale logical profile [stale1234]"
    stale_members = [f"{logical_name} :: {index}" for index in ("old-a", "old-b")]
    raw = {
        "name": logical_name,
        "_fwrouter_runtime_name": logical_name,
        "_fwrouter_topology": {
            "kind": "logical_profile",
            "runtime_policy": "fallback",
            "endpoints": [
                {"identity": f"sub:{suffix}", "runtime": {
                    "type": "socks5", "server": f"{suffix}.example.invalid", "port": 1080,
                }}
                for suffix in ("old-a", "old-b")
            ],
        },
    }
    with db_session() as connection:
        connection.execute(
            "INSERT INTO servers (server_id, server_name, provider_name, raw_json, inventory_state) "
            "VALUES (?, ?, 'subscription', ?, 'missing')",
            (logical_id, "Stale logical profile", json.dumps(raw)),
        )

    exact_generated = {
        "name": logical_name,
        "type": "fallback",
        "proxies": stale_members,
        "url": "https://www.gstatic.com/generate_204",
        "interval": mihomo_proxies_service.LOGICAL_PROFILE_GROUP_INTERVAL_SECONDS,
    }
    edited_same_name = {**exact_generated, "proxies": [*stale_members, "DIRECT"]}
    unknown_owner = {
        **exact_generated,
        "name": "Unknown logical profile [unknown1]",
        "proxies": ["Unknown logical profile [unknown1] :: member-x"],
    }
    unrelated = {
        "name": "user-selected-egress",
        "type": "select",
        "proxies": ["DIRECT", "vpn-auto"],
    }
    exact_groups = mihomo_proxies_service._ensure_selector_groups({
        "proxies": [], "proxy-groups": [exact_generated, unrelated],
    })
    exact_names = {group["name"] for group in exact_groups}
    assert logical_name not in exact_names
    assert {group["name"]: group for group in exact_groups}["user-selected-egress"] == unrelated

    edited_groups = mihomo_proxies_service._ensure_selector_groups({
        "proxies": [], "proxy-groups": [edited_same_name, unknown_owner, unrelated],
    })
    by_name = {group["name"]: group for group in edited_groups}
    assert by_name[logical_name] == edited_same_name
    assert by_name[unknown_owner["name"]] == unknown_owner
    assert by_name["user-selected-egress"] == unrelated
    assert "DIRECT" in by_name["user-selected-egress"]["proxies"]

    referenced_groups = mihomo_proxies_service._ensure_selector_groups({
        "proxies": [], "proxy-groups": [exact_generated],
        "rules": [f"DOMAIN,example.test,{logical_name},no-resolve"],
    })
    assert {group["name"]: group for group in referenced_groups}[logical_name] == exact_generated

    custom_reference = {
        "name": "operator-profile-selector",
        "type": "select",
        "proxies": [logical_name, "DIRECT"],
    }
    custom_reference_groups = mihomo_proxies_service._ensure_selector_groups({
        "proxies": [], "proxy-groups": [exact_generated, custom_reference],
    })
    custom_reference_by_name = {group["name"]: group for group in custom_reference_groups}
    assert custom_reference_by_name[logical_name] == exact_generated
    assert custom_reference_by_name["operator-profile-selector"] == custom_reference

    stale_auto_reference = {
        "name": "vpn-auto", "type": "select", "proxies": [logical_name, "REJECT"],
    }
    rebuilt_groups = mihomo_proxies_service._ensure_selector_groups({
        "proxies": [], "proxy-groups": [exact_generated, stale_auto_reference],
    })
    rebuilt_by_name = {group["name"]: group for group in rebuilt_groups}
    assert logical_name not in rebuilt_by_name
    assert logical_name not in rebuilt_by_name["vpn-auto"]["proxies"]


def test_mihomo_rebuild_replaces_active_profile_group_and_preserves_unrelated_selectors(
    monkeypatch, tmp_path: Path,
) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()
    logical_id = "sub:refreshed-logical-profile"
    logical_name = "Refreshed logical profile [fresh1234]"
    raw = {
        "name": logical_name,
        "_fwrouter_runtime_name": logical_name,
        "_fwrouter_topology": {
            "kind": "logical_profile",
            "runtime_policy": "fallback",
            "endpoints": [
                {"identity": "sub:new-a", "runtime": {
                    "type": "socks5", "server": "new-a.example.invalid", "port": 1080,
                }},
                {"identity": "sub:new-b", "runtime": {
                    "type": "socks5", "server": "new-b.example.invalid", "port": 1080,
                }},
            ],
        },
    }
    with db_session() as connection:
        connection.execute(
            "INSERT INTO servers (server_id, server_name, provider_name, raw_json, inventory_state) "
            "VALUES (?, ?, 'subscription', ?, 'active')",
            (logical_id, "Refreshed logical profile", json.dumps(raw)),
        )
        connection.execute(
            "INSERT INTO server_preferences (server_id, vpn_auto, global_list) VALUES (?, 1, 1)",
            (logical_id,),
        )

    old_group = {
        "name": logical_name,
        "type": "fallback",
        "proxies": [f"{logical_name} :: old-a", f"{logical_name} :: old-b"],
        "url": "https://www.gstatic.com/generate_204",
        "interval": mihomo_proxies_service.LOGICAL_PROFILE_GROUP_INTERVAL_SECONDS,
    }
    custom = {"name": "operator-custom", "type": "select", "proxies": ["DIRECT", "vpn-auto"]}
    base = {"proxies": [], "proxy-groups": [old_group, custom]}

    proxies = mihomo_proxies_service._merge_runtime_proxies(base)
    groups = mihomo_proxies_service._ensure_selector_groups({**base, "proxies": proxies})
    by_name = {group["name"]: group for group in groups}
    expected_members = [f"{logical_name} :: new-a", f"{logical_name} :: new-b"]

    assert [proxy["name"] for proxy in proxies] == expected_members
    assert by_name[logical_name]["type"] == "fallback"
    assert by_name[logical_name]["proxies"] == expected_members
    assert not set(old_group["proxies"]) & set(by_name[logical_name]["proxies"])
    assert by_name["operator-custom"] == custom


@pytest.mark.parametrize("use_relational_members", [False, True])
def test_mihomo_retains_inactive_profile_only_for_last_good_handoff_then_prunes_it(
    monkeypatch, tmp_path: Path, use_relational_members: bool,
) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()
    old_id = "sub:handoff-old-profile"
    old_name = "Old handoff profile [old12345]"
    old_a = f"{old_name} :: old-a"
    old_b = f"{old_name} :: old-b"
    old_raw = {
        "name": old_name,
        "type": "vless",
        "server": "old-profile.example.invalid",
        "port": 443,
        "uuid": "6ba7b810-9dad-11d1-80b4-00c04fd430c8",
        "_fwrouter_runtime_name": old_name,
        "_fwrouter_server_id": old_id,
        "_fwrouter_topology": {
            "kind": "logical_profile",
            "runtime_policy": "fallback",
            "endpoints": [
                {"identity": "sub:old-a", "runtime": {
                    "type": "vless", "server": "old-a.example.invalid", "port": 443,
                    "uuid": "6ba7b810-9dad-11d1-80b4-00c04fd430c8", "network": "tcp",
                    "tls": True, "security": "reality",
                }},
                {"identity": "sub:old-b", "runtime": {
                    "type": "vless", "server": "old-b.example.invalid", "port": 443,
                    "uuid": "6ba7b811-9dad-11d1-80b4-00c04fd430c8", "network": "grpc",
                    "tls": True, "security": "reality",
                }},
            ],
        },
    }
    new_id = "sub:handoff-new-profile"
    new_name = "New handoff profile [new12345]"
    new_raw = {
        "name": new_name,
        "_fwrouter_runtime_name": new_name,
        "_fwrouter_topology": {
            "kind": "logical_profile",
            "runtime_policy": "fallback",
            "endpoints": [
                {"identity": "sub:new-a", "runtime": {
                    "type": "socks5", "server": "new-a.example.invalid", "port": 1080,
                }},
            ],
        },
    }
    with db_session() as connection:
        connection.execute(
            "INSERT INTO servers (server_id, server_name, provider_name, raw_json, inventory_state) "
            "VALUES (?, ?, 'subscription', ?, 'missing')",
            (old_id, "Old handoff profile", json.dumps(old_raw)),
        )
        connection.execute(
            "INSERT INTO servers (server_id, server_name, provider_name, raw_json, inventory_state) "
            "VALUES (?, ?, 'subscription', ?, 'active')",
            (new_id, "New handoff profile", json.dumps(new_raw)),
        )
        connection.execute(
            "INSERT INTO server_preferences (server_id, vpn_auto, global_list) VALUES (?, 1, 1)",
            (new_id,),
        )
        if use_relational_members:
            connection.execute(
                "INSERT INTO logical_server_topology "
                "(logical_server_id, topology_kind, selection_policy) VALUES (?, 'logical_multi', 'fallback')",
                (old_id,),
            )
            for index, (member_id, runtime_name, host) in enumerate((
                ("old-a", old_a, "old-a.example.invalid"),
                ("old-b", old_b, "old-b.example.invalid"),
            )):
                connection.execute(
                    "INSERT INTO logical_server_members "
                    "(logical_server_id, member_id, member_runtime_name, member_config_json, "
                    "transport_fingerprint, member_order, is_active) VALUES (?, ?, ?, ?, ?, ?, 1)",
                    (old_id, member_id, runtime_name,
                     json.dumps({"type": "vless", "server": host, "port": 443,
                                 "uuid": f"6ba7b81{index}-9dad-11d1-80b4-00c04fd430c8",
                                 "network": "tcp" if index == 0 else "grpc",
                                 "tls": True, "security": "reality"}),
                     f"fingerprint-{index}", index),
                )

    old_group = {
        "name": old_name,
        "type": "fallback",
        "proxies": [old_a, old_b],
        "url": "https://www.gstatic.com/generate_204",
        "interval": mihomo_proxies_service.LOGICAL_PROFILE_GROUP_INTERVAL_SECONDS,
    }
    old_proxy_a = {
        "name": old_a, "type": "vless", "server": "old-a.example.invalid", "port": 443,
        "uuid": "6ba7b810-9dad-11d1-80b4-00c04fd430c8", "network": "tcp",
        "tls": True, "security": "reality",
    }
    old_proxy_b = {
        "name": old_b, "type": "vless", "server": "old-b.example.invalid", "port": 443,
        "uuid": "6ba7b811-9dad-11d1-80b4-00c04fd430c8", "network": "grpc",
        "tls": True, "security": "reality",
    }
    base = {"proxies": [old_proxy_a, old_proxy_b], "proxy-groups": [old_group]}

    during_handoff = mihomo_proxies_service._merge_runtime_proxies(
        base, required_last_good_names={old_name}, required_last_good_server_ids={old_id},
    )
    during_names = {proxy["name"] for proxy in during_handoff}
    assert old_a in during_names and old_b in during_names
    assert old_name not in during_names  # The profile owner is a group, not a duplicate native proxy.
    assert f"{new_name} :: new-a" in during_names
    during_groups = mihomo_proxies_service._ensure_selector_groups({
        **base, "proxies": during_handoff,
    })
    during_by_name = {group["name"]: group for group in during_groups}
    assert during_by_name[old_name] == old_group
    assert during_by_name[new_name]["proxies"] == [f"{new_name} :: new-a"]

    missing_member_base = {"proxies": [old_proxy_a], "proxy-groups": [old_group]}
    missing_member_names = {
        proxy["name"] for proxy in mihomo_proxies_service._merge_runtime_proxies(
            missing_member_base,
            required_last_good_names={old_name},
            required_last_good_server_ids={old_id},
        )
    }
    assert old_a in missing_member_names
    assert old_b in missing_member_names  # The exact persisted profile member is enough for its applied handoff.
    assert old_name not in missing_member_names

    absent_old_group_base = {"proxies": [], "proxy-groups": []}
    retained_members = mihomo_proxies_service._merge_runtime_proxies(
        absent_old_group_base,
        required_last_good_names={old_name},
        required_last_good_server_ids={old_id},
    )
    retained_groups = {group["name"]: group for group in absent_old_group_base["proxy-groups"]}
    assert {proxy["name"] for proxy in retained_members} >= {old_a, old_b}
    assert retained_groups[old_name] == old_group

    after_convergence = mihomo_proxies_service._merge_runtime_proxies(
        base, required_last_good_names=set(),
    )
    after_names = {proxy["name"] for proxy in after_convergence}
    assert old_a not in after_names and old_b not in after_names
    assert f"{new_name} :: new-a" in after_names
    after_groups = mihomo_proxies_service._ensure_selector_groups({
        **base, "proxies": after_convergence,
    })
    after_by_name = {group["name"]: group for group in after_groups}
    assert old_name not in after_by_name
    assert after_by_name[new_name]["proxies"] == [f"{new_name} :: new-a"]


def test_mihomo_refuses_conflicting_inactive_handoff_group(monkeypatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()
    old_id = "sub:handoff-conflicting-old-profile"
    old_name = "Old conflicting handoff profile [old98765]"
    old_raw = {
        "name": old_name,
        "_fwrouter_runtime_name": old_name,
        "_fwrouter_topology": {
            "kind": "logical_profile",
            "runtime_policy": "fallback",
            "endpoints": [{"identity": "sub:old", "runtime": {
                "type": "socks5", "server": "old.example.invalid", "port": 1080,
            }}],
        },
    }
    custom_group = {"name": old_name, "type": "select", "proxies": ["DIRECT"]}
    with db_session() as connection:
        connection.execute(
            "INSERT INTO servers (server_id, server_name, provider_name, raw_json, inventory_state) "
            "VALUES (?, ?, 'subscription', ?, 'missing')",
            (old_id, "Old conflicting handoff profile", json.dumps(old_raw)),
        )
    base = {"proxies": [], "proxy-groups": [custom_group]}
    try:
        mihomo_proxies_service._merge_runtime_proxies(
            base, required_last_good_names={old_name}, required_last_good_server_ids={old_id},
        )
    except ValueError as exc:
        assert str(exc) == "MIHOMO_XRAY_HANDOFF_TARGET_OWNERSHIP_AMBIGUOUS"
    else:
        raise AssertionError("ambiguous same-name handoff group must fail closed")
    assert base["proxy-groups"] == [custom_group]


def test_xray_handoff_candidate_restores_required_missing_server_proxy(monkeypatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()
    monkeypatch.setattr(
        mihomo_proxies_service,
        "resolve_mihomo_runtime_proxy_rows",
        lambda **_kwargs: [],
    )
    old_proxy = {
        "name": "Old Runtime Name [deadbeef]",
        "type": "vless",
        "server": "203.0.113.42",
        "port": 443,
        "uuid": "old-uuid",
    }
    expected_proxy = {**old_proxy, "udp": True}
    with db_session() as connection:
        connection.execute(
            """
            INSERT INTO servers (server_id, server_name, provider_name, raw_json, inventory_state)
            VALUES ('server-old', 'Old Runtime Name', 'pytest', ?, 'missing')
            """,
            (json.dumps(old_proxy),),
        )

    proxies = mihomo_proxies_service._merge_runtime_proxies(
        {"proxies": []},
        required_last_good_names={"Old Runtime Name [deadbeef]"},
        required_last_good_server_ids={"server-old"},
    )

    assert proxies == [expected_proxy]


def _seed_fingerprint_servers() -> None:
    with db_session() as connection:
        for server_id in ("auto-a", "auto-b", "fixed-a"):
            connection.execute(
                "INSERT INTO servers (server_id, server_name, provider_name, raw_json, inventory_state) "
                "VALUES (?, ?, 'pytest', '{}', 'active')",
                (server_id, server_id),
            )
            connection.execute(
                "INSERT INTO server_preferences (server_id, vpn_auto, global_list) VALUES (?, 0, 1)",
                (server_id,),
            )
        connection.execute(
            """INSERT OR IGNORE INTO routing_global_state
               (id, desired_mode, applied_mode, selective_default, server_mode)
               VALUES (1, 'direct', 'direct', 'direct', 'auto')"""
        )
        updated = connection.execute(
            "UPDATE routing_global_state SET active_auto_server_id='auto-a' WHERE id=1"
        )
        assert updated.rowcount == 1
        seeded = connection.execute(
            "SELECT active_auto_server_id FROM routing_global_state WHERE id=1"
        ).fetchone()
        assert seeded is not None and seeded["active_auto_server_id"] == "auto-a"
        connection.execute("DELETE FROM settings WHERE key='vpn_auto_exclusive_source_ref'")


def test_mihomo_fingerprint_ignores_derived_auto_selection_but_tracks_generation_inputs(
    monkeypatch,
    tmp_path: Path,
) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()
    _seed_fingerprint_servers()
    source_fingerprint = {"mihomo_config.py": "source-before"}
    monkeypatch.setattr(
        mihomo_reconcile_fingerprint_service,
        "_source_fingerprint",
        lambda: dict(source_fingerprint),
    )
    routing = {
        "desired_mode": "vpn",
        "applied_mode": "vpn",
        "selective_default": "vpn",
        "server_mode": "auto",
        "desired_fixed_server_id": None,
        "applied_fixed_server_id": None,
        "active_auto_server_id": "auto-a",
    }
    original_routing = dict(routing)
    baseline = current_mihomo_input_fingerprint(routing)
    assert routing == original_routing
    assert baseline["payload"]["routing"] == {
        "desired_mode": "vpn",
        "applied_mode": "vpn",
        "selective_default": "vpn",
        "server_mode": "auto",
        "desired_fixed_server_id": None,
        "applied_fixed_server_id": None,
    }
    assert "active_auto_server_id" not in baseline["payload"]["tables"]["routing_global_state"][0]
    changed_optional_intent = current_mihomo_input_fingerprint(
        {**routing, "selective_default": "direct"}
    )
    assert changed_optional_intent["hash"] != baseline["hash"]

    with db_session() as connection:
        updated = connection.execute(
            "UPDATE routing_global_state SET active_auto_server_id='auto-b' WHERE id=1"
        )
        selected = connection.execute(
            "SELECT active_auto_server_id FROM routing_global_state WHERE id=1"
        ).fetchone()
    assert updated.rowcount == 1
    assert selected is not None and selected["active_auto_server_id"] == "auto-b"
    selection_only_routing = {**routing, "active_auto_server_id": "auto-b"}
    assert current_mihomo_input_fingerprint(selection_only_routing)["hash"] == baseline["hash"]
    assert selection_only_routing["active_auto_server_id"] == "auto-b"

    before_fixed_intent = current_mihomo_input_fingerprint(selection_only_routing)["hash"]
    with db_session() as connection:
        connection.execute(
            "UPDATE routing_global_state SET server_mode='fixed', desired_fixed_server_id='fixed-a' WHERE id=1"
        )
    assert current_mihomo_input_fingerprint(selection_only_routing)["hash"] != before_fixed_intent

    before_exclusive_membership = current_mihomo_input_fingerprint(selection_only_routing)["hash"]
    with db_session() as connection:
        connection.execute(
            "INSERT INTO settings (key, value_json) VALUES ('vpn_auto_exclusive_source_ref', ?)",
            (json.dumps({"source_ref": "src:" + "a" * 64}),),
        )
    assert current_mihomo_input_fingerprint(selection_only_routing)["hash"] != before_exclusive_membership

    before_vpn_auto_membership = current_mihomo_input_fingerprint(selection_only_routing)["hash"]
    with db_session() as connection:
        connection.execute("UPDATE server_preferences SET vpn_auto=1 WHERE server_id='auto-b'")
    assert current_mihomo_input_fingerprint(selection_only_routing)["hash"] != before_vpn_auto_membership

    before_server_source = current_mihomo_input_fingerprint(selection_only_routing)["hash"]
    with db_session() as connection:
        connection.execute(
            "UPDATE servers SET raw_json=? WHERE server_id='auto-b'",
            (json.dumps({"changed": True}),),
        )
    assert current_mihomo_input_fingerprint(selection_only_routing)["hash"] != before_server_source

    before_source_change = current_mihomo_input_fingerprint(selection_only_routing)["hash"]
    source_fingerprint["mihomo_config.py"] = "source-after"
    assert current_mihomo_input_fingerprint(selection_only_routing)["hash"] != before_source_change


def test_mihomo_publication_fence_rejects_newer_selection_with_stable_derived_input_hash(
    monkeypatch,
    tmp_path: Path,
) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()
    _seed_fingerprint_servers()
    before = current_mihomo_input_fingerprint({"active_auto_server_id": "auto-a"})
    with db_session() as connection:
        initial_revision = read_selection_revision(connection)
        updated = connection.execute(
            "UPDATE routing_global_state SET active_auto_server_id='auto-b' WHERE id=1"
        )
        next_revision = advance_selection_revision(connection, expected_revision=initial_revision)
        selected = connection.execute(
            "SELECT active_auto_server_id FROM routing_global_state WHERE id=1"
        ).fetchone()
    assert updated.rowcount == 1
    assert selected is not None and selected["active_auto_server_id"] == "auto-b"
    assert next_revision == initial_revision + 1
    after = current_mihomo_input_fingerprint({"active_auto_server_id": "auto-b"})
    assert after["hash"] == before["hash"]
    monkeypatch.setattr(mihomo_reconcile_service, "_mihomo_incarnation", lambda: "fixed-runtime-incarnation")
    monkeypatch.setattr(mihomo_reconcile_service, "_file_hash", lambda _path: "fixed-active-config")

    with xray_writer_guard(timeout_seconds=1.0):
        assert mihomo_reconcile_service._selection_publication_still_owned(
            operation_id="expected-operation",
            expected_revision=next_revision,
            expected_incarnation="fixed-runtime-incarnation",
            expected_input_hash=after["hash"],
            expected_active_hash="fixed-active-config",
            routing={"active_auto_server_id": "auto-b"},
            verification={
                "operation_id": "expected-operation",
                "selection_revision": next_revision,
            },
        ) is True
        assert mihomo_reconcile_service._selection_publication_still_owned(
            operation_id="expected-operation",
            expected_revision=initial_revision,
            expected_incarnation="fixed-runtime-incarnation",
            expected_input_hash=before["hash"],
            expected_active_hash="fixed-active-config",
            routing={"active_auto_server_id": "auto-a"},
            verification=None,
        ) is False
        assert mihomo_reconcile_service._selection_publication_still_owned(
            operation_id="expected-operation",
            expected_revision=next_revision,
            expected_incarnation="fixed-runtime-incarnation",
            expected_input_hash=after["hash"],
            expected_active_hash="fixed-active-config",
            routing={"active_auto_server_id": "auto-b"},
            verification={
                "operation_id": "different-operation",
                "selection_revision": next_revision,
            },
        ) is False


def test_checkpoint_policy_safe_loaders_preserve_failclosed_decisions(monkeypatch) -> None:
    from fwrouter_api.services import mihomo_reconcile as service
    import yaml

    safe = yaml.safe_dump({
        "proxy-groups": [
            {"name": "vpn-auto", "proxies": ["REJECT"]},
            {"name": "vpn-global", "proxies": ["vpn-auto", "REJECT"]},
        ],
        "rules": ["DOMAIN-SUFFIX,example.test,vpn-global",
                  "AND,((NETWORK,udp),(DOMAIN-SUFFIX,example.test)),REJECT"],
        "sub-rules": {
            "fwrouter-transparent": ["MATCH,DIRECT"],
            "fwrouter-full-vpn": ["MATCH,vpn-global", "NETWORK,udp,REJECT"],
        },
    })
    cases = (safe, "rules: [", "!!python/object/apply:builtins.eval ['1+1']", "- not-a-mapping")
    expected = ((True, True), (False, True), (False, True), (False, True))
    def decisions():
        return tuple((service._config_has_failclosed_udp_policy(text),
                      service._config_has_mandatory_vpn_routes(text, routing={"desired_mode": "vpn"}))
                     for text in cases)
    assert decisions() == expected
    monkeypatch.delattr(yaml, "CSafeLoader", raising=False)
    assert decisions() == expected


def test_real_reconcile_forwards_separate_fenced_fixed_materialization_callback(monkeypatch, tmp_path: Path) -> None:
    _assert_reconcile_mihomo_runtime_skips_restart_for_unchanged_config(
        monkeypatch, tmp_path, format_only_candidate=False, native_proof_ok=True,
        verification_callback_ok=True, fixed_restart_wiring=True,
    )


@pytest.mark.parametrize("core_apply_state", ["clean", "pending"])
def test_fresh_fixed_structural_reconcile_reserves_promotes_and_publishes_receipt(
    monkeypatch, tmp_path: Path, core_apply_state: str,
) -> None:
    """Connect fresh R→R+1 reservation, real promotion, restart seam and receipt in SQLite."""
    import hashlib

    from fwrouter_api.services import core_intent, mihomo_config_paths, mihomo_runtime
    from fwrouter_api.services.mihomo_reconcile import _file_hash
    from fwrouter_api.services.server_global_selection import (
        _capture_fixed_apply_context,
        _fixed_generation_callbacks,
        _fixed_materialization_callback,
        set_global_fixed_server,
    )
    from fwrouter_api.services.xray_vpn_auto_pending import _load_row

    _configure_env(monkeypatch, tmp_path)
    initialize_database()
    with db_session() as connection:
        connection.execute(
            "UPDATE modules SET desired_state='enabled', lifecycle_mode='managed', apply_state=? "
            "WHERE module_name='core'",
            (core_apply_state,),
        )
        connection.execute(
            "INSERT INTO servers(server_id, server_name, provider_name, raw_json, inventory_state) "
            "VALUES ('fresh-fixed-owner', 'Runtime fresh-fixed-owner', 'pytest', ?, 'active')",
            (json.dumps({"name": "Runtime fresh-fixed-owner", "type": "socks5", "server": "198.18.0.11", "port": 12345}),),
        )

    base_path = tmp_path / "mihomo-base.yaml"
    candidate_path = tmp_path / "mihomo-candidate.yaml"
    last_good_path = tmp_path / "mihomo-last-good"
    before_bytes = b"rules:\n  - MATCH,REJECT\n"
    candidate_bytes = b"rules:\n  - MATCH,DIRECT\n"
    base_path.write_bytes(before_bytes)
    runtime_id = {"value": "fresh-fixed-before"}
    monkeypatch.setattr(core_intent, "read_core_runtime_context", lambda: {
        "boot_id": "00000000-0000-0000-0000-000000000001",
        "network_namespace_inode": "1",
    })
    monkeypatch.setattr(mihomo_runtime, "get_mihomo_runtime_incarnation", lambda **_kwargs: runtime_id["value"])
    monkeypatch.setattr(mihomo_reconcile_service, "_mihomo_incarnation", lambda: runtime_id["value"])
    monkeypatch.setattr(mihomo_reconcile_fingerprint_service, "_current_runtime_incarnation", lambda: runtime_id["value"])
    monkeypatch.setattr(mihomo_config_paths, "_resolved_base_config_path", lambda: str(base_path))
    monkeypatch.setattr(mihomo_config_paths, "_resolved_candidate_config_path", lambda: str(candidate_path))
    monkeypatch.setattr(mihomo_config_service, "_resolved_base_config_path", lambda: str(base_path))
    monkeypatch.setattr(mihomo_config_service, "_resolved_candidate_config_path", lambda: str(candidate_path))
    monkeypatch.setattr(mihomo_config_service, "_resolved_last_good_mihomo_dir", lambda: last_good_path)
    monkeypatch.setattr(mihomo_reconcile_fingerprint_service, "_resolved_base_config_path", lambda: str(base_path))
    monkeypatch.setattr(mihomo_reconcile_fingerprint_service, "_resolved_candidate_config_path", lambda: str(candidate_path))
    monkeypatch.setattr(mihomo_config_service, "managed_runtime_operation_blocked", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(mihomo_reconcile_service, "current_mihomo_input_fingerprint", lambda _routing: {"hash": "a" * 64})
    monkeypatch.setattr(mihomo_reconcile_service, "mihomo_input_unchanged", lambda _fingerprint: False)
    monkeypatch.setattr(
        "fwrouter_api.services.subscription._subscription_inventory_generation_gate",
        lambda _admission, **_kwargs: {"ok": True},
    )
    monkeypatch.setattr(mihomo_reconcile_service, "_inventory_candidate_promotion_blocked", lambda _admission: None)
    def write_candidate(_routing=None):
        candidate_path.write_bytes(candidate_bytes)
        return {"candidate_path": str(candidate_path), "rules": ["MATCH,DIRECT"]}

    monkeypatch.setattr(mihomo_config_service, "write_mihomo_candidate_config", write_candidate)
    monkeypatch.setattr(mihomo_config_service, "validate_mihomo_candidate_config", lambda *_args, **_kwargs: {"ok": True})
    monkeypatch.setattr(mihomo_config_service, "_summarize_candidate", lambda _candidate: {"rules_count": 1})
    monkeypatch.setattr(mihomo_reconcile_service, "_native_udp_policy_proof", lambda: {
        "ok": True,
        "native_top_level_rules_match": True,
        "selector_membership_matches": True,
        "guarded_rules_match": True,
    })
    monkeypatch.setattr(
        mihomo_config_status_service, "mihomo_runtime_fixed_target_materialized",
        lambda _routing, **_kwargs: {
            "ok": True,
            "fixed_runtime_target": "Runtime fresh-fixed-owner",
            "config_membership_ok": True,
            "native_membership_ok": True,
            "logical_group_native_membership_ok": True,
            "vpn_global_now": "REJECT",
            "selected_fixed_target": False,
        },
    )

    desired = set_global_fixed_server("fresh-fixed-owner", requested_by="pytest")
    assert desired["ok"] is True
    token = _capture_fixed_apply_context(desired["routing"])
    assert token is not None
    source_revision = token["selection_revision"]
    reserve, publish = _fixed_generation_callbacks(desired["routing"], token)
    phases: list[str] = []

    def restart(**kwargs):
        assert kwargs["selection_fenced"] is True
        assert kwargs["expected_selection_revision"] == source_revision + 1
        assert kwargs["operation_id"]
        assert kwargs["generation_verification_callback"] is materialize
        runtime_id["value"] = "fresh-fixed-after-restart"
        proof = kwargs["generation_verification_callback"](
            operation_id=kwargs["operation_id"],
            expected_selection_revision=kwargs["expected_selection_revision"],
        )
        assert proof["ok"] is True, proof
        phases.append("restart_readback")
        return {"ok": True, "action": kwargs["action"]}

    monkeypatch.setattr(mihomo_config_service, "restart_mihomo_container", restart)
    materialize = _fixed_materialization_callback(desired["routing"], token)
    result = mihomo_reconcile_service.reconcile_mihomo_runtime(
        desired["routing"],
        job_id="fresh-fixed-reconcile",
        verification_callback=materialize,
        generation_materialization_callback=materialize,
        generation_reservation_callback=reserve,
        generation_completion_callback=publish,
        _inventory_admission=object(),
    )

    assert result["ok"] is True, result
    assert result["promoted"]["promoted"] is True
    assert result["container"]["action"] == "force_recreate"
    assert result["reentry_generation_fence"]["before_revision"] == source_revision
    assert result["reentry_generation_fence"]["selection_revision"] == source_revision + 1
    assert phases == ["restart_readback"]
    assert _file_hash(str(base_path)) == hashlib.sha256(candidate_bytes).hexdigest()
    assert mihomo_reconcile_fingerprint_service._load_state()["active_config_hash"] == _file_hash(str(base_path))
    with db_session() as connection:
        assert read_selection_revision(connection) == source_revision + 1
        marker = _load_row(connection)
    assert marker["workflow"]["phase"] == "mihomo_generation_verified"
    reservation = marker["workflow"]["mihomo_generation_reservation"]
    receipt = marker["workflow"]["mihomo_generation_receipt"]
    assert reservation["before_revision"] == source_revision
    assert reservation["selection_revision"] == source_revision + 1
    assert receipt["operation_id"] == reservation["operation_id"]
    assert receipt["active_config_hash"] == _file_hash(str(base_path))


@pytest.mark.parametrize("restart_already_happened", [False, True])
def test_fixed_generation_resume_uses_active_effect_phase_without_unknown_reload(
    monkeypatch, tmp_path: Path, restart_already_happened: bool,
) -> None:
    """The real reconcile path retries an unfinished restart, but never reloads UNKNOWN."""
    import hashlib

    from fwrouter_api.services.mihomo_reconcile import _write_fixed_generation_checkpoint

    _configure_env(monkeypatch, tmp_path)
    initialize_database()
    _enable_vpn_module()
    monkeypatch.setattr(mihomo_config_service, "_resolved_last_good_mihomo_dir", lambda: tmp_path / "last-good")
    runtime_id = {"value": "mihomo-before-fixed-operation"}
    monkeypatch.setattr("fwrouter_api.services.mihomo_reconcile._mihomo_incarnation", lambda: runtime_id["value"])
    monkeypatch.setattr(mihomo_reconcile_fingerprint_service, "_current_runtime_incarnation", lambda: runtime_id["value"])
    monkeypatch.setattr(mihomo_reconcile_service, "current_mihomo_input_fingerprint", lambda _routing: {"hash": "a" * 64})
    monkeypatch.setattr(mihomo_reconcile_service, "mihomo_input_unchanged", lambda _fingerprint: False)
    monkeypatch.setattr(
        "fwrouter_api.services.subscription._subscription_inventory_generation_gate",
        lambda _admission, **_kwargs: {"ok": True},
    )

    base_path = Path(mihomo_config_service._resolved_base_config_path())
    candidate_path = Path(mihomo_config_service._resolved_candidate_config_path())
    candidate_text = yaml.safe_dump({"rules": ["MATCH,DIRECT"]}, sort_keys=False)
    candidate_bytes = candidate_text.encode("utf-8")
    base_path.parent.mkdir(parents=True, exist_ok=True)
    base_path.write_bytes(candidate_bytes)
    def write_candidate(_routing=None):
        candidate_path.parent.mkdir(parents=True, exist_ok=True)
        candidate_path.write_bytes(candidate_bytes)
        return {"candidate_path": str(candidate_path), "rules": ["MATCH,DIRECT"]}

    monkeypatch.setattr(mihomo_config_service, "write_mihomo_candidate_config", write_candidate)
    monkeypatch.setattr(mihomo_config_service, "validate_mihomo_candidate_config", lambda *_args, **_kwargs: {"ok": True})
    monkeypatch.setattr(mihomo_config_service, "_summarize_candidate", lambda _candidate: {"rules_count": 1})
    monkeypatch.setattr(mihomo_reconcile_service, "_build_config_status_summary", lambda **_kwargs: {})
    monkeypatch.setattr(mihomo_reconcile_service, "_native_udp_policy_proof", lambda: pytest.fail("UNKNOWN readback must not be replaced by a native proof/write"))
    operation_id = "8ab7f539-32af-4cf9-acf3-b75d48b43b7d"
    checkpoint = _write_fixed_generation_checkpoint(operation_id, b"before generation\n")
    assert checkpoint is not None
    with db_session() as connection:
        selection_revision = advance_selection_revision(
            connection, expected_revision=read_selection_revision(connection),
        )
    assert selection_revision is not None
    before_hash = hashlib.sha256(b"before generation\n").hexdigest()
    candidate_hash = hashlib.sha256(candidate_bytes).hexdigest()
    reservation = {
        "schema": "mihomo-fixed-generation-reservation/v1",
        "operation_id": operation_id,
        "job_id": "fixed-resume-test",
        "before_revision": selection_revision - 1,
        "selection_revision": selection_revision,
        "input_fingerprint_hash": "a" * 64,
        "candidate_config_hash": candidate_hash,
        "before_active_config_hash": before_hash,
        "before_runtime_incarnation": "mihomo-before-fixed-operation",
        "checkpoint_ref": checkpoint,
    }
    if restart_already_happened:
        runtime_id["value"] = "mihomo-after-fixed-operation"

    phases: list[str] = []

    def validate_reservation(*, mode: str, expected_selection_revision: int, reservation: dict, **_proof):
        assert mode == "validate"
        phases.append("reservation_validated")
        with db_session() as connection:
            assert read_selection_revision(connection) == expected_selection_revision == selection_revision
        assert reservation == reservation_expected
        return {"ok": True, "before_revision": reservation["before_revision"], "selection_revision": selection_revision}

    reservation_expected = reservation

    def verification(*, operation_id: str, expected_selection_revision: int):
        phases.append("readback_attempted")
        return {"ok": False, "operation_id": operation_id, "selection_revision": expected_selection_revision}

    def restart(**_kwargs):
        phases.append("restart_attempted")
        return {"ok": True}

    monkeypatch.setattr(mihomo_config_service, "restart_mihomo_container", restart)
    monkeypatch.setattr(mihomo_reconcile_service, "promote_mihomo_candidate_config", lambda **_kwargs: {"ok": True, "promoted": True})

    if restart_already_happened:
        result = mihomo_reconcile_service.reconcile_mihomo_runtime(
            {"server_mode": "fixed", "desired_fixed_server_id": "fixed-resume-target"},
            job_id="fixed-resume-test",
            verification_callback=verification,
            generation_materialization_callback=verification,
            generation_reservation_callback=validate_reservation,
            generation_completion_callback=lambda **_kwargs: pytest.fail("unproven readback must not publish a receipt"),
            fixed_generation_reservation=reservation,
        )
        assert result["ok"] is False
        assert result["stage"] == "verification"
        assert result["last_good_retained"] is False
        assert result["checkpoint_retained"] is True
        assert phases == ["reservation_validated", "readback_attempted"]
    else:
        monkeypatch.setattr(
            mihomo_config_service, "restart_mihomo_container",
            lambda **_kwargs: phases.append("restart_attempted") or (_ for _ in ()).throw(RuntimeError("RESUME_RESTART_REACHED")),
        )
        with pytest.raises(RuntimeError, match="RESUME_RESTART_REACHED"):
            mihomo_reconcile_service.reconcile_mihomo_runtime(
                {"server_mode": "fixed", "desired_fixed_server_id": "fixed-resume-target"},
                job_id="fixed-resume-test",
                verification_callback=verification,
                generation_materialization_callback=verification,
                generation_reservation_callback=validate_reservation,
                generation_completion_callback=lambda **_kwargs: pytest.fail("restart seam must precede receipt"),
                fixed_generation_reservation=reservation,
            )
        assert phases == ["reservation_validated", "reservation_validated", "restart_attempted"]
