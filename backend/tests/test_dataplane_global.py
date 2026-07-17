from __future__ import annotations

from pathlib import Path

from fwrouter_api.adapters.mihomo import MihomoHealth, MihomoRuntimeState
from fwrouter_api.core.config import get_settings
from fwrouter_api.db.connection import db_session, initialize_database
from fwrouter_api.services.dataplane_global import (
    MISSING_DNSMASQ_DOMAIN_SELECTIVE,
    MISSING_MIHOMO_TPROXY,
    MISSING_MIHOMO_TRANSPARENT_CONTOUR,
    MISSING_VPN_TPROXY_HANDOFF,
    build_global_preflight,
    build_nft_rule_sets,
    read_effective_rules_artifact,
)
from fwrouter_api.services.live_probe_cache import clear_live_probe_cache


def _configure_env(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("FWROUTER_STATE_DIR", str(tmp_path / "state"))
    get_settings.cache_clear()


def test_build_nft_rule_sets_protects_custom_proxy_ip(monkeypatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()

    with db_session() as connection:
        connection.execute(
            """
            INSERT INTO servers (
                server_id,
                server_name,
                provider_name,
                inventory_state
            )
            VALUES (?, ?, ?, ?)
            """,
            (
                "custom-https:test:deadbeef",
                "Test Proxy",
                "pytest",
                "active",
            ),
        )
        connection.execute(
            """
            INSERT INTO server_custom_https_proxy (
                server_id,
                host,
                port,
                username,
                password,
                tls,
                sni,
                skip_cert_verify,
                path,
                proxy_type
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "custom-https:test:deadbeef",
                "161.0.21.33",
                8000,
                "user",
                "pass",
                0,
                "",
                0,
                "",
                "socks5",
            ),
        )

    nft_sets = build_nft_rule_sets(None)

    assert "161.0.21.33/32" in nft_sets["protected_ipv4"]
    assert "192.168.0.0/16" in nft_sets["protected_ipv4"]


def test_read_effective_rules_artifact_falls_back_to_last_good(
    monkeypatch,
    tmp_path: Path,
) -> None:
    _configure_env(monkeypatch, tmp_path)
    clear_live_probe_cache()
    paths = get_settings().paths
    last_good_rules = paths.state_dir / "last-good" / "rules"
    last_good_rules.mkdir(parents=True, exist_ok=True)
    (last_good_rules / "effective-rules.json").write_text(
        '{"selective_default":"direct","rules":[{"kind":"domain","action":"VPN","value":"instagram.com"}]}',
        encoding="utf-8",
    )

    artifact = read_effective_rules_artifact()

    assert artifact is not None
    assert artifact["rules"][0]["value"] == "instagram.com"


def test_build_nft_rule_sets_includes_local_interface_networks(monkeypatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()
    monkeypatch.setattr(
        "fwrouter_api.services.dataplane_global._discover_local_interface_protected_networks",
        lambda: (["198.19.77.0/24"], ["2001:db8:77::/64"]),
    )

    nft_sets = build_nft_rule_sets(None)

    assert "198.19.77.0/24" in nft_sets["protected_ipv4"]
    assert "2001:db8:77::/64" in nft_sets["protected_ipv6"]
def test_build_nft_rule_sets_keeps_big_vpn_ip_aggregates(
    monkeypatch,
    tmp_path: Path,
) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()

    nft_sets = build_nft_rule_sets(
        {
            "selective_default": "vpn",
            "rules": [
                {"action": "VPN", "kind": "ip", "value": "8.6.112.0", "source": "big_vpn"},
                {"action": "VPN", "kind": "cidr", "value": "8.47.69.0/32", "source": "big_vpn"},
            ],
        }
    )

    assert "8.6.112.0/32" in nft_sets["vpn_ipv4"]
    assert "8.47.69.0/32" in nft_sets["vpn_ipv4"]


def test_build_nft_rule_sets_forces_android_connectivity_domains_direct(
    monkeypatch,
    tmp_path: Path,
) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()

    nft_sets = build_nft_rule_sets(
        {
            "selective_default": "direct",
            "rules": [
                {"action": "VPN", "kind": "domain", "value": "connectivitycheck.gstatic.com", "source": "big_vpn"},
                {"action": "VPN", "kind": "domain", "value": "clients3.google.com", "source": "big_vpn"},
                {"action": "VPN", "kind": "domain", "value": "www.gstatic.com", "source": "big_vpn"},
            ],
        }
    )

    assert "connectivitycheck.gstatic.com" in nft_sets["direct_domains"]
    assert "clients3.google.com" in nft_sets["direct_domains"]
    assert "www.gstatic.com" in nft_sets["direct_domains"]
    assert "connectivitycheck.gstatic.com" not in nft_sets["vpn_domains"]
    assert "clients3.google.com" not in nft_sets["vpn_domains"]
    assert "www.gstatic.com" not in nft_sets["vpn_domains"]


def test_build_global_preflight_marks_domain_aware_selective_missing_dnsmasq_contract(
    monkeypatch,
    tmp_path: Path,
) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()
    monkeypatch.setattr(
        "fwrouter_api.services.dataplane_global._mihomo_health",
        lambda: MihomoHealth(
            runtime_state=MihomoRuntimeState.RUNNING,
            active_server_id=None,
            message="ok",
            details={
                "config": {
                    "redir_port": 5202,
                    "tproxy_port": 5203,
                    "tun_enabled": True,
                    "fwrouter_contours": {
                        "explicit_proxy": {"preserved": True},
                        "transparent_vpn": {"ready": True, "isolated_from_explicit_proxy": True, "redir_port": 5202, "tproxy_port": 5203},
                        "domain_selective": {"ready": True},
                    },
                },
                "selectors": {
                    "vpn_global_exists": True,
                    "vpn_global_targets_count": 1,
                    "vpn_global_has_vpn_auto": True,
                },
            },
        ),
    )
    monkeypatch.setattr(
        "fwrouter_api.services.dnsmasq.inspect_dnsmasq_selective_status",
        lambda: {"ok": False, "missing": ["dhcp_dns_force_mismatch"]},
    )

    preflight = build_global_preflight(
        routing={"desired_mode": "selective", "applied_mode": "selective", "selective_default": "direct"},
        check_details={"required_chains": {"prerouting": True, "output": True, "forward": True, "postrouting": True}},
        effective_rules_artifact={
            "selective_default": "direct",
            "rules": [{"action": "VPN", "kind": "domain", "value": "example.com"}],
        },
        require_runtime_verify=False,
    )

    assert preflight["selective_rules"]["path_kind"] == "domain_aware"
    assert MISSING_DNSMASQ_DOMAIN_SELECTIVE in preflight["missing_by_mode"]["selective"]
    assert preflight["can_enforce_global_selective"] is False
    assert preflight["dnsmasq_selective_status"]["ok"] is False


def test_build_global_preflight_marks_transparent_contour_missing_for_vpn_selective(
    monkeypatch,
    tmp_path: Path,
) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()
    monkeypatch.setattr(
        "fwrouter_api.services.dataplane_global._mihomo_health",
        lambda: MihomoHealth(
            runtime_state=MihomoRuntimeState.DEGRADED,
            active_server_id=None,
            message="transparent contour invalid",
            details={
                "config": {
                    "redir_port": 5202,
                    "tproxy_port": 5203,
                    "tun_enabled": True,
                    "fwrouter_contours": {
                        "explicit_proxy": {"preserved": True},
                        "transparent_vpn": {
                            "ready": False,
                            "isolated_from_explicit_proxy": True,
                            "redir_port": 5202,
                            "tproxy_port": 5203,
                            "listener_loopback_bound": True,
                            "listener_bind_valid": False,
                        },
                        "domain_selective": {"ready": True, "uses_transparent_contour": True},
                    },
                },
                "selectors": {
                    "vpn_global_exists": True,
                    "vpn_global_targets_count": 1,
                    "vpn_global_has_vpn_auto": True,
                },
            },
        ),
    )
    monkeypatch.setattr(
        "fwrouter_api.services.dnsmasq.inspect_dnsmasq_selective_status",
        lambda: {"ok": True, "missing": []},
    )

    preflight = build_global_preflight(
        routing={"desired_mode": "selective", "applied_mode": "selective", "selective_default": "direct"},
        check_details={"required_chains": {"prerouting": True, "output": True, "forward": True, "postrouting": True}},
        effective_rules_artifact={
            "selective_default": "direct",
            "rules": [{"action": "VPN", "kind": "domain", "value": "example.com"}],
        },
        require_runtime_verify=False,
    )

    assert MISSING_MIHOMO_TRANSPARENT_CONTOUR in preflight["missing_by_mode"]["vpn"]
    assert MISSING_MIHOMO_TRANSPARENT_CONTOUR in preflight["missing"]
    assert preflight["can_enforce_global_selective"] is True
    assert preflight["can_enforce_global_vpn"] is False
    assert preflight["selective_vpn_ready"] is False
    assert preflight["selective_degraded"] is True


def test_build_global_preflight_marks_missing_tproxy_handoff_when_mark_seen_without_handoff(
    monkeypatch,
    tmp_path: Path,
) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()
    monkeypatch.setattr(
        "fwrouter_api.services.dataplane_global._mihomo_health",
        lambda: MihomoHealth(
            runtime_state=MihomoRuntimeState.RUNNING,
            active_server_id="vpn-auto",
            message="ok",
            details={
                "config": {
                    "redir_port": 5202,
                    "tproxy_port": 5203,
                    "tun_enabled": True,
                    "fwrouter_contours": {
                        "explicit_proxy": {"preserved": True},
                        "transparent_vpn": {"ready": True, "isolated_from_explicit_proxy": True, "redir_port": 5202, "tproxy_port": 5203},
                        "domain_selective": {"ready": True},
                    },
                },
                "selectors": {
                    "vpn_global_exists": True,
                    "vpn_global_targets_count": 1,
                    "vpn_global_has_vpn_auto": True,
                },
            },
        ),
    )
    monkeypatch.setattr(
        "fwrouter_api.services.dnsmasq.inspect_dnsmasq_selective_status",
        lambda: {"ok": True, "missing": []},
    )

    preflight = build_global_preflight(
        routing={"desired_mode": "vpn", "applied_mode": "vpn", "selective_default": "direct"},
        check_details={
            "required_chains": {"prerouting": True, "output": True, "forward": True, "postrouting": True},
            "transparent_path": {
                "vpn_mark_packets": 12,
                "vpn_mark_tcp_packets": 12,
                "tproxy_handoff_packets": 0,
                "redirect_handoff_tcp_packets": 0,
                "transparent_tcp_flow_observed": False,
                "transparent_flow_observed": False,
                "failure_stage": "vpn_mark_tcp_without_redirect_handoff",
            },
        },
        mihomo_health=MihomoHealth(
            runtime_state=MihomoRuntimeState.RUNNING,
            active_server_id="vpn-auto",
            message="ok",
            details={
                "config": {
                    "redir_port": 5202,
                    "tproxy_port": 5203,
                    "tun_enabled": True,
                    "fwrouter_contours": {
                        "explicit_proxy": {"preserved": True},
                        "transparent_vpn": {
                            "ready": True,
                            "isolated_from_explicit_proxy": True,
                            "redir_port": 5202,
                            "tproxy_port": 5203,
                            "transparent_tcp_listener_present": True,
                            "transparent_udp_listener_present": True,
                            "transparent_tcp_ready": True,
                            "transparent_udp_ready": True,
                        },
                        "domain_selective": {"ready": True},
                    },
                },
                "selectors": {
                    "vpn_global_exists": True,
                    "vpn_global_targets_count": 1,
                    "vpn_global_has_vpn_auto": True,
                },
                "transparent_runtime": {
                    "transparent_tcp_session_materialized": False,
                    "transparent_udp_session_materialized": False,
                },
            },
        ),
        effective_rules_artifact={
            "selective_default": "direct",
            "rules": [{"action": "VPN", "kind": "domain", "value": "example.com"}],
        },
        require_runtime_verify=True,
    )

    assert MISSING_VPN_TPROXY_HANDOFF in preflight["missing_by_mode"]["vpn"]
    assert preflight["transparent_path"]["failure_stage"] == "vpn_mark_tcp_without_redirect_handoff"
    assert preflight["transparent_path"]["transparent_tcp_session_materialized"] is False


def test_build_global_preflight_requires_canonical_transparent_tcp_listener_for_selective(
    monkeypatch,
    tmp_path: Path,
) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()
    monkeypatch.setattr(
        "fwrouter_api.services.dnsmasq.inspect_dnsmasq_selective_status",
        lambda: {"ok": True, "missing": []},
    )

    preflight = build_global_preflight(
        routing={"desired_mode": "selective", "applied_mode": "selective", "selective_default": "direct"},
        mihomo_health=MihomoHealth(
            runtime_state=MihomoRuntimeState.RUNNING,
            active_server_id="vpn-auto",
            message="legacy top-level ports only",
            details={
                "config": {
                    "redir_port": 5202,
                    "tproxy_port": 5203,
                    "tun_enabled": True,
                    "fwrouter_contours": {},
                },
                "selectors": {
                    "vpn_global_exists": True,
                    "vpn_global_targets_count": 1,
                    "vpn_global_has_vpn_auto": True,
                },
            },
        ),
        effective_rules_artifact={
            "selective_default": "direct",
            "rules": [{"action": "VPN", "kind": "domain", "value": "example.com"}],
        },
    )

    assert MISSING_MIHOMO_TRANSPARENT_CONTOUR in preflight["missing_by_mode"]["vpn"]
    assert preflight["mihomo_contours"]["transparent_tcp_listener_present"] is False
    assert preflight["mihomo_contours"]["transparent_tcp_ready"] is False


def test_build_global_preflight_uses_contour_tproxy_port_when_controller_is_degraded(
    monkeypatch,
    tmp_path: Path,
) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()
    monkeypatch.setattr(
        "fwrouter_api.services.dataplane_global._mihomo_health",
        lambda: MihomoHealth(
            runtime_state=MihomoRuntimeState.DEGRADED,
            active_server_id=None,
            message="controller unauthorized but generated config is present",
            details={
                "config": {
                    "tproxy_port": None,
                    "redir_port": None,
                    "tun_enabled": False,
                    "fwrouter_contours": {
                        "explicit_proxy": {"preserved": True},
                        "transparent_vpn": {
                            "ready": True,
                            "isolated_from_explicit_proxy": True,
                            "redir_port": 5202,
                            "tproxy_port": 5203,
                        },
                        "domain_selective": {
                            "ready": True,
                            "uses_transparent_contour": True,
                            "explicit_proxy_preserved": True,
                        },
                    },
                },
                "selectors": {},
                "error": "401 Unauthorized",
            },
        ),
    )
    monkeypatch.setattr(
        "fwrouter_api.services.dnsmasq.inspect_dnsmasq_selective_status",
        lambda: {"ok": True, "missing": []},
    )

    preflight = build_global_preflight(
        routing={"desired_mode": "direct", "applied_mode": "direct", "selective_default": "direct"},
        check_details={"required_chains": {"prerouting": True, "output": True, "forward": True, "postrouting": True}},
        effective_rules_artifact={
            "selective_default": "direct",
            "rules": [{"action": "VPN", "kind": "domain", "value": "example.com"}],
        },
        require_runtime_verify=False,
    )

    assert preflight["profile"]["mihomo"]["redir_port"] == 5202
    assert preflight["profile"]["mihomo"]["tproxy_port"] == 5203
    assert preflight["profile"]["vpn_routing_contract"]["redir_port"] == 5202
    assert preflight["profile"]["vpn_routing_contract"]["tproxy_port"] == 5203
    assert preflight["vpn_contour"]["redir_port"] == 5202
    assert preflight["vpn_contour"]["tproxy_port"] == 5203
    assert MISSING_MIHOMO_TPROXY not in preflight["missing_by_mode"]["vpn"]
