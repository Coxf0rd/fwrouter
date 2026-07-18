from __future__ import annotations

import subprocess
from pathlib import Path

from fwrouter_api.services import dnsmasq as dnsmasq_service
from fwrouter_api.services.live_probe_cache import clear_live_probe_cache


def test_inspect_dnsmasq_selective_status_requires_dns_capture(monkeypatch, tmp_path: Path) -> None:
    clear_live_probe_cache()
    rules_path = tmp_path / "fwrouter-rules.conf"
    dhcp_path = tmp_path / "fwrouter-dhcp-dns.conf"
    ipv6_lan_path = tmp_path / "fwrouter-ipv6-lan.conf"
    local_hosts_path = tmp_path / "fwrouter-local-hosts.conf"
    rules_path.write_text("# managed\n", encoding="utf-8")
    dhcp_path.write_text("dhcp-option-force=option:dns-server,192.168.0.1\n", encoding="utf-8")
    ipv6_lan_path.write_text("filter-AAAA\n", encoding="utf-8")
    local_hosts_path.write_text("address=/fwrouter.lan/192.168.0.1\n", encoding="utf-8")

    monkeypatch.setattr(dnsmasq_service, "DNSMASQ_RULES_CONF_PATH", rules_path)
    monkeypatch.setattr(dnsmasq_service, "DNSMASQ_DHCP_DNS_CONF_PATH", dhcp_path)
    monkeypatch.setattr(dnsmasq_service, "DNSMASQ_IPV6_LAN_CONF_PATH", ipv6_lan_path)
    monkeypatch.setattr(dnsmasq_service, "DNSMASQ_LOCAL_HOSTS_CONF_PATH", local_hosts_path)
    monkeypatch.setattr(
        dnsmasq_service,
        "_discover_router_dns_bindings",
        lambda: [{"ifname": "enp2s0", "address": "192.168.0.1"}],
    )
    monkeypatch.setattr(
        dnsmasq_service,
        "inspect_dns_capture_status",
        lambda: {
            "ok": False,
            "interfaces": ["enp2s0"],
            "bindings": [{"ifname": "enp2s0", "address": "192.168.0.1"}],
            "rules_present": [{"ifname": "enp2s0", "address": "192.168.0.1", "udp": False, "tcp": False}],
            "missing": ["dns_capture_udp_missing:enp2s0:192.168.0.1", "dns_capture_tcp_missing:enp2s0:192.168.0.1"],
        },
    )
    monkeypatch.setattr(
        dnsmasq_service,
        "_probe_dnsmasq_nftset_materialization",
        lambda: {"ok": True, "missing": [], "probes": [], "probe_count": 0, "restart_recommended": False, "status": "not_applicable"},
    )

    result = dnsmasq_service.inspect_dnsmasq_selective_status()

    assert result["ok"] is False
    assert result["forced_dns_configured"] is True
    assert result["router_dns_interfaces"] == ["enp2s0"]
    assert "dns_capture_udp_missing:enp2s0:192.168.0.1" in result["missing"]
    assert "dns_capture_tcp_missing:enp2s0:192.168.0.1" in result["missing"]


def test_reconcile_dnsmasq_rules_fails_when_dns_capture_enforcement_fails(
    monkeypatch,
    tmp_path: Path,
) -> None:
    clear_live_probe_cache()
    rules_path = tmp_path / "fwrouter-rules.conf"
    dhcp_path = tmp_path / "fwrouter-dhcp-dns.conf"
    ipv6_lan_path = tmp_path / "fwrouter-ipv6-lan.conf"
    local_hosts_path = tmp_path / "fwrouter-local-hosts.conf"
    monkeypatch.setattr(dnsmasq_service, "DNSMASQ_RULES_CONF_PATH", rules_path)
    monkeypatch.setattr(dnsmasq_service, "DNSMASQ_DHCP_DNS_CONF_PATH", dhcp_path)
    monkeypatch.setattr(dnsmasq_service, "DNSMASQ_IPV6_LAN_CONF_PATH", ipv6_lan_path)
    monkeypatch.setattr(dnsmasq_service, "DNSMASQ_LOCAL_HOSTS_CONF_PATH", local_hosts_path)
    monkeypatch.setattr(
        dnsmasq_service,
        "read_effective_rules_artifact",
        lambda: {"rules": [{"action": "VPN", "kind": "domain", "value": "example.com"}]},
    )
    monkeypatch.setattr(
        dnsmasq_service,
        "_discover_router_dns_bindings",
        lambda: [{"ifname": "enp2s0", "address": "192.168.0.1"}],
    )
    monkeypatch.setattr(
        dnsmasq_service,
        "ensure_dns_capture_rules",
        lambda: {
            "ok": False,
            "interfaces": ["enp2s0"],
            "bindings": [{"ifname": "enp2s0", "address": "192.168.0.1"}],
            "errors": ["enp2s0:192.168.0.1:iptables failed"],
            "status": {"ok": False, "missing": ["dns_capture_udp_missing:enp2s0:192.168.0.1"]},
        },
    )

    result = dnsmasq_service.reconcile_dnsmasq_rules()

    assert result["ok"] is False
    assert result["error_code"] == "DNSMASQ_DNS_CAPTURE_FAILED"
    assert result["dns_capture"]["ok"] is False


def test_reconcile_dnsmasq_rules_reports_dns_capture_success(
    monkeypatch,
    tmp_path: Path,
) -> None:
    clear_live_probe_cache()
    rules_path = tmp_path / "fwrouter-rules.conf"
    dhcp_path = tmp_path / "fwrouter-dhcp-dns.conf"
    ipv6_lan_path = tmp_path / "fwrouter-ipv6-lan.conf"
    local_hosts_path = tmp_path / "fwrouter-local-hosts.conf"
    monkeypatch.setattr(dnsmasq_service, "DNSMASQ_RULES_CONF_PATH", rules_path)
    monkeypatch.setattr(dnsmasq_service, "DNSMASQ_DHCP_DNS_CONF_PATH", dhcp_path)
    monkeypatch.setattr(dnsmasq_service, "DNSMASQ_IPV6_LAN_CONF_PATH", ipv6_lan_path)
    monkeypatch.setattr(dnsmasq_service, "DNSMASQ_LOCAL_HOSTS_CONF_PATH", local_hosts_path)
    monkeypatch.setattr(
        dnsmasq_service,
        "read_effective_rules_artifact",
        lambda: {"rules": [{"action": "VPN", "kind": "domain", "value": "example.com"}]},
    )
    monkeypatch.setattr(
        dnsmasq_service,
        "_discover_router_dns_bindings",
        lambda: [{"ifname": "enp2s0", "address": "192.168.0.1"}],
    )
    monkeypatch.setattr(
        dnsmasq_service,
        "ensure_dns_capture_rules",
        lambda: {
            "ok": True,
            "interfaces": ["enp2s0"],
            "bindings": [{"ifname": "enp2s0", "address": "192.168.0.1"}],
            "inserted": [{"ifname": "enp2s0", "address": "192.168.0.1", "udp_changed": True, "tcp_changed": True}],
            "errors": [],
            "status": {"ok": True, "missing": []},
        },
    )
    monkeypatch.setattr(
        dnsmasq_service,
        "inspect_dnsmasq_selective_status",
        lambda: {
            "ok": True,
            "missing": [],
            "router_dns_ipv4": ["192.168.0.1"],
            "router_dns_interfaces": ["enp2s0"],
            "nftset_probe_status": {"ok": True, "missing": [], "restart_recommended": False},
        },
    )

    calls: list[list[str]] = []

    def fake_run(argv: list[str], check: bool, capture_output: bool, text: bool = False):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(dnsmasq_service.subprocess, "run", fake_run)

    result = dnsmasq_service.reconcile_dnsmasq_rules()

    assert result["ok"] is True
    assert result["dns_capture"]["ok"] is True
    assert any(argv[:3] == ["systemctl", "restart", "dnsmasq"] for argv in calls)
    assert result["restart_reason"] == "config_changed"
    assert result["ipv6_lan_changed"] is True
    assert result["local_hosts_changed"] is True
    assert rules_path.read_text(encoding="utf-8").splitlines() == [
        "# Generated by FWRouter v2",
        "# Decision logic is in the core; dnsmasq populates nft sets and applies DNS upstream overrides.",
        "",
        f"nftset=/connectivitycheck.gstatic.com/4#{dnsmasq_service.DNSMASQ_NFTSET_TABLE}#{dnsmasq_service.DNSMASQ_DIRECT_IPV4_SET}",
        f"nftset=/connectivitycheck.android.com/4#{dnsmasq_service.DNSMASQ_NFTSET_TABLE}#{dnsmasq_service.DNSMASQ_DIRECT_IPV4_SET}",
        f"nftset=/clients3.google.com/4#{dnsmasq_service.DNSMASQ_NFTSET_TABLE}#{dnsmasq_service.DNSMASQ_DIRECT_IPV4_SET}",
        f"nftset=/clients.l.google.com/4#{dnsmasq_service.DNSMASQ_NFTSET_TABLE}#{dnsmasq_service.DNSMASQ_DIRECT_IPV4_SET}",
        f"nftset=/www.google.com/4#{dnsmasq_service.DNSMASQ_NFTSET_TABLE}#{dnsmasq_service.DNSMASQ_DIRECT_IPV4_SET}",
        f"nftset=/www.gstatic.com/4#{dnsmasq_service.DNSMASQ_NFTSET_TABLE}#{dnsmasq_service.DNSMASQ_DIRECT_IPV4_SET}",
        "server=/example.com/1.1.1.1",
        f"nftset=/example.com/4#{dnsmasq_service.DNSMASQ_NFTSET_TABLE}#{dnsmasq_service.DNSMASQ_VPN_IPV4_SET}",
    ]
    assert ipv6_lan_path.read_text(encoding="utf-8").splitlines() == [
        "# Generated by FWRouter v2",
        "# LAN clients must stay IPv4-only; do not hand AAAA answers to local DNS clients.",
        "",
        "filter-AAAA",
    ]
    assert local_hosts_path.read_text(encoding="utf-8").splitlines() == [
        "# Generated by FWRouter v2",
        "# Local LAN ingress hostnames. HTTP Host routing is handled by reverse proxy.",
        "",
        "address=/fwrouter.lan/192.168.0.1",
        "address=/homes.lan/192.168.0.1",
    ]


def test_reconcile_dnsmasq_rules_forces_android_connectivity_domains_direct(
    monkeypatch,
    tmp_path: Path,
) -> None:
    clear_live_probe_cache()
    rules_path = tmp_path / "fwrouter-rules.conf"
    dhcp_path = tmp_path / "fwrouter-dhcp-dns.conf"
    ipv6_lan_path = tmp_path / "fwrouter-ipv6-lan.conf"
    local_hosts_path = tmp_path / "fwrouter-local-hosts.conf"
    monkeypatch.setattr(dnsmasq_service, "DNSMASQ_RULES_CONF_PATH", rules_path)
    monkeypatch.setattr(dnsmasq_service, "DNSMASQ_DHCP_DNS_CONF_PATH", dhcp_path)
    monkeypatch.setattr(dnsmasq_service, "DNSMASQ_IPV6_LAN_CONF_PATH", ipv6_lan_path)
    monkeypatch.setattr(dnsmasq_service, "DNSMASQ_LOCAL_HOSTS_CONF_PATH", local_hosts_path)
    monkeypatch.setattr(
        dnsmasq_service,
        "read_effective_rules_artifact",
        lambda: {"rules": [{"action": "VPN", "kind": "domain", "value": "connectivitycheck.gstatic.com"}]},
    )
    monkeypatch.setattr(
        dnsmasq_service,
        "_discover_router_dns_bindings",
        lambda: [{"ifname": "enp2s0", "address": "192.168.0.1"}],
    )
    monkeypatch.setattr(
        dnsmasq_service,
        "ensure_dns_capture_rules",
        lambda: {
            "ok": True,
            "interfaces": ["enp2s0"],
            "bindings": [{"ifname": "enp2s0", "address": "192.168.0.1"}],
            "inserted": [],
            "errors": [],
            "status": {"ok": True, "missing": []},
        },
    )
    monkeypatch.setattr(
        dnsmasq_service,
        "inspect_dnsmasq_selective_status",
        lambda: {
            "ok": True,
            "missing": [],
            "router_dns_ipv4": ["192.168.0.1"],
            "router_dns_interfaces": ["enp2s0"],
            "nftset_probe_status": {"ok": True, "missing": [], "restart_recommended": False},
        },
    )
    monkeypatch.setattr(
        dnsmasq_service.subprocess,
        "run",
        lambda argv, check, capture_output, text=False: subprocess.CompletedProcess(argv, 0, "", ""),
    )

    result = dnsmasq_service.reconcile_dnsmasq_rules()

    assert result["ok"] is True
    assert rules_path.read_text(encoding="utf-8").splitlines() == [
        "# Generated by FWRouter v2",
        "# Decision logic is in the core; dnsmasq populates nft sets and applies DNS upstream overrides.",
        "",
        f"nftset=/connectivitycheck.gstatic.com/4#{dnsmasq_service.DNSMASQ_NFTSET_TABLE}#{dnsmasq_service.DNSMASQ_DIRECT_IPV4_SET}",
        f"nftset=/connectivitycheck.android.com/4#{dnsmasq_service.DNSMASQ_NFTSET_TABLE}#{dnsmasq_service.DNSMASQ_DIRECT_IPV4_SET}",
        f"nftset=/clients3.google.com/4#{dnsmasq_service.DNSMASQ_NFTSET_TABLE}#{dnsmasq_service.DNSMASQ_DIRECT_IPV4_SET}",
        f"nftset=/clients.l.google.com/4#{dnsmasq_service.DNSMASQ_NFTSET_TABLE}#{dnsmasq_service.DNSMASQ_DIRECT_IPV4_SET}",
        f"nftset=/www.google.com/4#{dnsmasq_service.DNSMASQ_NFTSET_TABLE}#{dnsmasq_service.DNSMASQ_DIRECT_IPV4_SET}",
        f"nftset=/www.gstatic.com/4#{dnsmasq_service.DNSMASQ_NFTSET_TABLE}#{dnsmasq_service.DNSMASQ_DIRECT_IPV4_SET}",
    ]


def test_reconcile_dnsmasq_rules_restarts_when_nftset_probe_is_unhealthy(
    monkeypatch,
    tmp_path: Path,
) -> None:
    clear_live_probe_cache()
    rules_path = tmp_path / "fwrouter-rules.conf"
    dhcp_path = tmp_path / "fwrouter-dhcp-dns.conf"
    ipv6_lan_path = tmp_path / "fwrouter-ipv6-lan.conf"
    local_hosts_path = tmp_path / "fwrouter-local-hosts.conf"
    rules_path.write_text(
        "\n".join(
            [
                "# Generated by FWRouter v2",
                "# Decision logic is in the core; dnsmasq populates nft sets and applies DNS upstream overrides.",
                "",
                f"nftset=/connectivitycheck.gstatic.com/4#{dnsmasq_service.DNSMASQ_NFTSET_TABLE}#{dnsmasq_service.DNSMASQ_DIRECT_IPV4_SET}",
                f"nftset=/connectivitycheck.android.com/4#{dnsmasq_service.DNSMASQ_NFTSET_TABLE}#{dnsmasq_service.DNSMASQ_DIRECT_IPV4_SET}",
                f"nftset=/clients3.google.com/4#{dnsmasq_service.DNSMASQ_NFTSET_TABLE}#{dnsmasq_service.DNSMASQ_DIRECT_IPV4_SET}",
                f"nftset=/clients.l.google.com/4#{dnsmasq_service.DNSMASQ_NFTSET_TABLE}#{dnsmasq_service.DNSMASQ_DIRECT_IPV4_SET}",
                f"nftset=/www.google.com/4#{dnsmasq_service.DNSMASQ_NFTSET_TABLE}#{dnsmasq_service.DNSMASQ_DIRECT_IPV4_SET}",
                f"nftset=/www.gstatic.com/4#{dnsmasq_service.DNSMASQ_NFTSET_TABLE}#{dnsmasq_service.DNSMASQ_DIRECT_IPV4_SET}",
                "server=/example.com/1.1.1.1",
                f"nftset=/example.com/4#{dnsmasq_service.DNSMASQ_NFTSET_TABLE}#{dnsmasq_service.DNSMASQ_VPN_IPV4_SET}",
                "",
            ]
        ),
        encoding="utf-8",
    )
    dhcp_path.write_text(
        "\n".join(
            [
                "# Generated by FWRouter v2",
                "# Force LAN clients to use router DNS so domain-aware selective nft sets materialize.",
                "",
                "dhcp-option-force=option:dns-server,192.168.0.1",
                "",
            ]
        ),
        encoding="utf-8",
    )
    ipv6_lan_path.write_text(
        "\n".join(
            [
                "# Generated by FWRouter v2",
                "# LAN clients must stay IPv4-only; do not hand AAAA answers to local DNS clients.",
                "",
                "filter-AAAA",
                "",
            ]
        ),
        encoding="utf-8",
    )
    local_hosts_path.write_text(
        "\n".join(
            [
                "# Generated by FWRouter v2",
                "# Local LAN ingress hostnames. HTTP Host routing is handled by reverse proxy.",
                "",
                "address=/fwrouter.lan/192.168.0.1",
                "address=/homes.lan/192.168.0.1",
                "",
            ]
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(dnsmasq_service, "DNSMASQ_RULES_CONF_PATH", rules_path)
    monkeypatch.setattr(dnsmasq_service, "DNSMASQ_DHCP_DNS_CONF_PATH", dhcp_path)
    monkeypatch.setattr(dnsmasq_service, "DNSMASQ_IPV6_LAN_CONF_PATH", ipv6_lan_path)
    monkeypatch.setattr(dnsmasq_service, "DNSMASQ_LOCAL_HOSTS_CONF_PATH", local_hosts_path)
    monkeypatch.setattr(
        dnsmasq_service,
        "read_effective_rules_artifact",
        lambda: {"rules": [{"action": "VPN", "kind": "domain", "value": "example.com"}]},
    )
    monkeypatch.setattr(
        dnsmasq_service,
        "_discover_router_dns_bindings",
        lambda: [{"ifname": "enp2s0", "address": "192.168.0.1"}],
    )
    monkeypatch.setattr(
        dnsmasq_service,
        "ensure_dns_capture_rules",
        lambda: {
            "ok": True,
            "interfaces": ["enp2s0"],
            "bindings": [{"ifname": "enp2s0", "address": "192.168.0.1"}],
            "inserted": [],
            "errors": [],
            "status": {"ok": True, "missing": []},
        },
    )

    selective_statuses = iter(
        [
            {
                "ok": False,
                "missing": ["dnsmasq_nftset_probe_materialization_missing:vpn:example.com:203.0.113.10"],
                "router_dns_ipv4": ["192.168.0.1"],
                "router_dns_interfaces": ["enp2s0"],
                "nftset_probe_status": {
                    "ok": False,
                    "missing": ["dnsmasq_nftset_probe_materialization_missing:vpn:example.com:203.0.113.10"],
                    "restart_recommended": True,
                },
            },
            {
                "ok": True,
                "missing": [],
                "router_dns_ipv4": ["192.168.0.1"],
                "router_dns_interfaces": ["enp2s0"],
                "nftset_probe_status": {
                    "ok": True,
                    "missing": [],
                    "restart_recommended": False,
                },
            },
        ]
    )
    monkeypatch.setattr(dnsmasq_service, "inspect_dnsmasq_selective_status", lambda: next(selective_statuses))

    calls: list[list[str]] = []

    def fake_run(argv: list[str], check: bool, capture_output: bool, text: bool = False):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(dnsmasq_service.subprocess, "run", fake_run)

    result = dnsmasq_service.reconcile_dnsmasq_rules()

    assert result["ok"] is True
    assert result["restart_required"] is True
    assert result["restart_reason"] == "nftset_probe_unhealthy"
    assert any(argv[:3] == ["systemctl", "restart", "dnsmasq"] for argv in calls)


def test_reconcile_dnsmasq_rules_waits_for_nftset_probe_after_restart(
    monkeypatch,
    tmp_path: Path,
) -> None:
    clear_live_probe_cache()
    rules_path = tmp_path / "fwrouter-rules.conf"
    dhcp_path = tmp_path / "fwrouter-dhcp-dns.conf"
    ipv6_lan_path = tmp_path / "fwrouter-ipv6-lan.conf"
    local_hosts_path = tmp_path / "fwrouter-local-hosts.conf"
    rules_text = "\n".join(
        [
            "# Generated by FWRouter v2",
            "# Decision logic is in the core; dnsmasq populates nft sets and applies DNS upstream overrides.",
            "",
            f"nftset=/connectivitycheck.gstatic.com/4#{dnsmasq_service.DNSMASQ_NFTSET_TABLE}#{dnsmasq_service.DNSMASQ_DIRECT_IPV4_SET}",
            f"nftset=/connectivitycheck.android.com/4#{dnsmasq_service.DNSMASQ_NFTSET_TABLE}#{dnsmasq_service.DNSMASQ_DIRECT_IPV4_SET}",
            f"nftset=/clients3.google.com/4#{dnsmasq_service.DNSMASQ_NFTSET_TABLE}#{dnsmasq_service.DNSMASQ_DIRECT_IPV4_SET}",
            f"nftset=/clients.l.google.com/4#{dnsmasq_service.DNSMASQ_NFTSET_TABLE}#{dnsmasq_service.DNSMASQ_DIRECT_IPV4_SET}",
            f"nftset=/www.google.com/4#{dnsmasq_service.DNSMASQ_NFTSET_TABLE}#{dnsmasq_service.DNSMASQ_DIRECT_IPV4_SET}",
            f"nftset=/www.gstatic.com/4#{dnsmasq_service.DNSMASQ_NFTSET_TABLE}#{dnsmasq_service.DNSMASQ_DIRECT_IPV4_SET}",
            "server=/example.com/1.1.1.1",
            f"nftset=/example.com/4#{dnsmasq_service.DNSMASQ_NFTSET_TABLE}#{dnsmasq_service.DNSMASQ_VPN_IPV4_SET}",
            "",
        ]
    )
    rules_path.write_text(rules_text, encoding="utf-8")
    dhcp_path.write_text(
        "# Generated by FWRouter v2\n\n"
        "dhcp-option-force=option:dns-server,192.168.0.1\n",
        encoding="utf-8",
    )
    ipv6_lan_path.write_text("# Generated by FWRouter v2\n\nfilter-AAAA\n", encoding="utf-8")
    local_hosts_path.write_text(
        "# Generated by FWRouter v2\n\n"
        "address=/fwrouter.lan/192.168.0.1\n"
        "address=/homes.lan/192.168.0.1\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(dnsmasq_service, "DNSMASQ_RULES_CONF_PATH", rules_path)
    monkeypatch.setattr(dnsmasq_service, "DNSMASQ_DHCP_DNS_CONF_PATH", dhcp_path)
    monkeypatch.setattr(dnsmasq_service, "DNSMASQ_IPV6_LAN_CONF_PATH", ipv6_lan_path)
    monkeypatch.setattr(dnsmasq_service, "DNSMASQ_LOCAL_HOSTS_CONF_PATH", local_hosts_path)
    monkeypatch.setattr(dnsmasq_service, "DNSMASQ_RESTART_VERIFY_DELAY_SECONDS", 0)
    monkeypatch.setattr(
        dnsmasq_service,
        "read_effective_rules_artifact",
        lambda: {"rules": [{"action": "VPN", "kind": "domain", "value": "example.com"}]},
    )
    monkeypatch.setattr(
        dnsmasq_service,
        "_discover_router_dns_bindings",
        lambda: [{"ifname": "enp2s0", "address": "192.168.0.1"}],
    )
    monkeypatch.setattr(
        dnsmasq_service,
        "ensure_dns_capture_rules",
        lambda: {
            "ok": True,
            "interfaces": ["enp2s0"],
            "bindings": [{"ifname": "enp2s0", "address": "192.168.0.1"}],
            "inserted": [],
            "errors": [],
            "status": {"ok": True, "missing": []},
        },
    )

    unhealthy = {
        "ok": False,
        "missing": ["dnsmasq_nftset_probe_materialization_missing:vpn:example.com:203.0.113.10"],
        "router_dns_ipv4": ["192.168.0.1"],
        "router_dns_interfaces": ["enp2s0"],
        "nftset_probe_status": {
            "ok": False,
            "missing": ["dnsmasq_nftset_probe_materialization_missing:vpn:example.com:203.0.113.10"],
            "restart_recommended": True,
        },
    }
    healthy = {
        "ok": True,
        "missing": [],
        "router_dns_ipv4": ["192.168.0.1"],
        "router_dns_interfaces": ["enp2s0"],
        "nftset_probe_status": {"ok": True, "missing": [], "restart_recommended": False},
    }
    selective_statuses = iter([unhealthy, unhealthy, healthy])
    monkeypatch.setattr(dnsmasq_service, "inspect_dnsmasq_selective_status", lambda: next(selective_statuses))
    monkeypatch.setattr(
        dnsmasq_service.subprocess,
        "run",
        lambda argv, check, capture_output, text=False: subprocess.CompletedProcess(argv, 0, "", ""),
    )

    result = dnsmasq_service.reconcile_dnsmasq_rules()

    assert result["ok"] is True
    assert result["restart_reason"] == "config_changed"
    assert result["selective_status"]["ok"] is True
