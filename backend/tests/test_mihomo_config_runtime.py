from __future__ import annotations

import json
from pathlib import Path

import yaml

from fwrouter_api.core.config import get_settings
from fwrouter_api.db.connection import db_session, initialize_database
from fwrouter_api.services import mihomo_config as mihomo_config_service
from fwrouter_api.services.live_probe_cache import clear_live_probe_cache


def _configure_env(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("FWROUTER_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("FWROUTER_DATABASE_URL", f"sqlite:///{tmp_path}/fwrouter.db")
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


def _seed_runtime_proxy_server(
    server_id: str,
    *,
    server_name: str,
    vpn_auto: bool,
    global_list: bool,
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
                global_list
            )
            VALUES (?, ?, ?)
            """,
            (server_id, 1 if vpn_auto else 0, 1 if global_list else 0),
        )


def _base_candidate() -> dict:
    return {
        "allow-lan": True,
        "routing-mark": 512,
        "bind-address": mihomo_config_service.TRANSPARENT_BIND_ADDRESS,
        "rules": ["MATCH,DIRECT"],
        "proxies": [{"name": "Test VPN", "type": "socks5", "server": "1.1.1.1", "port": 1080}],
        "proxy-groups": [
            {"name": "vpn-auto", "type": "select", "proxies": ["Test VPN", "DIRECT"]},
            {"name": "vpn-global", "type": "select", "proxies": ["vpn-auto", "Test VPN", "DIRECT"]},
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
        "sub-rules": {"fwrouter-transparent": ["MATCH,vpn-global"]},
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
    assert managed["fwrouter-full-redir"]["proxy"] == "vpn-global"
    assert managed["fwrouter-full-redir"]["port"] == 5204
    assert "rule" not in managed["fwrouter-full-redir"]
    assert managed["fwrouter-full-tproxy"]["proxy"] == "vpn-global"
    assert managed["fwrouter-full-tproxy"]["port"] == 5205
    assert managed["fwrouter-full-tproxy"]["udp"] is True
    assert "rule" not in managed["fwrouter-full-tproxy"]
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
    assert config["sub-rules"]["fwrouter-transparent"][-1] == "MATCH,vpn-global"
    assert config["fwrouter"]["resolved_selective_default"] == "vpn"
    assert config["fwrouter"]["final_match_rule"] == "MATCH,DIRECT"
    assert config["fwrouter"]["transparent_final_match_rule"] == "MATCH,vpn-global"


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
        "DOMAIN,graph.facebook.com,vpn-global",
        "IP-CIDR,192.168.0.0/16,DIRECT",
        "MATCH,DIRECT",
    ]
    assert config["sub-rules"]["fwrouter-transparent"] == [
        "DOMAIN-SUFFIX,instagram.com,vpn-global",
        "DOMAIN,graph.facebook.com,vpn-global",
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
        "MATCH,DIRECT",
    ]
    assert config["sub-rules"]["fwrouter-transparent"] == [
        "DOMAIN-SUFFIX,instagram.com,vpn-global",
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
        "MATCH,DIRECT",
    ]
    assert "DOMAIN-SUFFIX,legacy.example,vpn-global" not in config["rules"]
    assert config["sub-rules"]["fwrouter-transparent"] == [
        "DOMAIN-SUFFIX,instagram.com,vpn-global",
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
        "MATCH,DIRECT",
    ]
    assert config["sub-rules"]["fwrouter-transparent"] == [
        "DOMAIN-SUFFIX,instagram.com,vpn-global",
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
    monkeypatch.setattr(mihomo_config_service, "_collect_xray_handoff_assignments", lambda: [])

    candidate_config = mihomo_config_service.build_mihomo_config({"selective_default": "direct"})
    restart_calls: list[str] = []

    def _fake_restart_mihomo_container(*, action: str = "restart", heartbeat=None):
        restart_calls.append(action)
        return {"ok": True, "action": action}

    monkeypatch.setattr(mihomo_config_service, "restart_mihomo_container", _fake_restart_mihomo_container)
    monkeypatch.setattr(mihomo_config_service, "get_mihomo_config_status", lambda **kwargs: {
        "base_config": candidate_config,
        "candidate_config": candidate_config,
        "base_path": mihomo_config_service.BASE_CONFIG_PATH,
        "candidate_path": mihomo_config_service.MIHOMO_CANDIDATE_CONFIG_PATH,
        "base_exists": True,
        "candidate_exists": True,
        "base_updated_at": "2026-06-08T00:00:00+00:00",
        "candidate_updated_at": "2026-06-08T00:00:00+00:00",
        "base_rules_count": len(candidate_config.get("rules") or []),
        "candidate_rules_count": len(candidate_config.get("rules") or []),
    })
    monkeypatch.setattr(mihomo_config_service, "write_mihomo_candidate_config", lambda routing=None: {
        "candidate_path": mihomo_config_service.MIHOMO_CANDIDATE_CONFIG_PATH,
        "rules": candidate_config["rules"],
        "handoff_assignments": [],
        "resolved_selective_default": "direct",
        "final_match_rule": "MATCH,DIRECT",
        "transparent_final_match_rule": "MATCH,DIRECT",
        "config": candidate_config,
    })
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
