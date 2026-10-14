from __future__ import annotations

import json
from pathlib import Path

import yaml

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
        f"AND,((NETWORK,udp),(AND,((SRC-IP-CIDR,192.168.0.51/32),(DOMAIN-SUFFIX,instagram.com)))),REJECT",
    ]
    assert transparent_rules[2:4] == [
        f"AND,((SRC-IP-CIDR,192.168.0.51/32),(IP-CIDR,203.0.113.0/24)),{selector_name}",
        f"AND,((NETWORK,udp),(AND,((SRC-IP-CIDR,192.168.0.51/32),(IP-CIDR,203.0.113.0/24)))),REJECT",
    ]
    assert f"AND,((SRC-IP-CIDR,192.168.0.51/32),(DOMAIN-SUFFIX,ru)),{selector_name}" not in transparent_rules
    assert not any(rule.startswith("SRC-IP-CIDR,192.168.0.51/32,") for rule in transparent_rules)
    assert transparent_rules[-1] == "MATCH,DIRECT"
    actual_full_vpn_rules = config["sub-rules"]["fwrouter-full-vpn"]
    expected_full_vpn_rules = [
        f"SRC-IP-CIDR,192.168.0.51/32,{selector_name}",
        f"AND,((NETWORK,udp),(SRC-IP-CIDR,192.168.0.51/32)),REJECT",
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


def test_reconcile_mihomo_runtime_skips_restart_for_unchanged_config(monkeypatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()
    monkeypatch.setattr("fwrouter_api.services.mihomo_reconcile._mihomo_incarnation", lambda: "pytest-runtime")
    monkeypatch.setattr(mihomo_reconcile_fingerprint_service, "_current_runtime_incarnation", lambda: "pytest-runtime")
    monkeypatch.setattr("fwrouter_api.services.mihomo_reconcile._native_udp_policy_proof", lambda: {
        "ok": True, "native_top_level_rules_match": True,
        "selector_membership_matches": True, "guarded_rules_match": True,
    })
    monkeypatch.setattr(mihomo_config_service, "_collect_xray_handoff_assignments", lambda: [])

    candidate_config = mihomo_config_service.build_mihomo_config({"selective_default": "direct"})
    base_path = Path(mihomo_config_service._resolved_base_config_path())
    candidate_path = Path(mihomo_config_service._resolved_candidate_config_path())
    _write_yaml(base_path, candidate_config)
    restart_calls: list[str] = []

    def _fake_restart_mihomo_container(*, action: str = "restart", heartbeat=None):
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
        _write_yaml(candidate_path, candidate_config)
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

    result = mihomo_config_service.reconcile_mihomo_runtime({"selective_default": "direct"})

    assert result["ok"] is True
    assert result["reconcile_action"] == "none"
    assert result["reconcile_reason"] == "unchanged_config"
    assert "config" not in (result["candidate"] or {})
    assert result["candidate"]["rules_count"] == len(candidate_config["rules"])
    assert "base_config" not in (result["config"] or {})
    assert "candidate_config" not in (result["config"] or {})
    assert restart_calls == []


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


def test_mihomo_retains_inactive_profile_only_for_last_good_handoff_then_prunes_it(
    monkeypatch, tmp_path: Path,
) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()
    old_id = "sub:handoff-old-profile"
    old_name = "Old handoff profile [old12345]"
    old_a = f"{old_name} :: old-a"
    old_b = f"{old_name} :: old-b"
    old_raw = {
        "name": old_name,
        "_fwrouter_runtime_name": old_name,
        "_fwrouter_topology": {
            "kind": "logical_profile",
            "runtime_policy": "fallback",
            "endpoints": [
                {"identity": "sub:old-a", "runtime": {
                    "type": "socks5", "server": "old-a.example.invalid", "port": 1080,
                }},
                {"identity": "sub:old-b", "runtime": {
                    "type": "socks5", "server": "old-b.example.invalid", "port": 1080,
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

    old_group = {
        "name": old_name,
        "type": "fallback",
        "proxies": [old_a, old_b],
        "url": "https://www.gstatic.com/generate_204",
        "interval": mihomo_proxies_service.LOGICAL_PROFILE_GROUP_INTERVAL_SECONDS,
    }
    old_proxy_a = {
        "name": old_a, "type": "socks5", "server": "old-a.example.invalid", "port": 1080,
    }
    base = {"proxies": [old_proxy_a], "proxy-groups": [old_group]}

    during_handoff = mihomo_proxies_service._merge_runtime_proxies(
        base, required_last_good_names={old_name},
    )
    during_names = {proxy["name"] for proxy in during_handoff}
    assert old_a in during_names
    assert old_b not in during_names  # Missing base member must never be synthesized.
    assert f"{new_name} :: new-a" in during_names
    during_groups = mihomo_proxies_service._ensure_selector_groups({
        **base, "proxies": during_handoff,
    })
    during_by_name = {group["name"]: group for group in during_groups}
    assert during_by_name[old_name] == old_group
    assert during_by_name[new_name]["proxies"] == [f"{new_name} :: new-a"]

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
