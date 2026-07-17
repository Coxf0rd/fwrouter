from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fwrouter_api.core.config import get_settings
from fwrouter_api.services.artifacts import atomic_copy_file, atomic_write_json, atomic_write_text
from fwrouter_api.services.logs import write_technical_log
from fwrouter_api.services.dataplane_global import build_nft_rule_sets, read_effective_rules_artifact


OWNED_TABLE = "inet fwrouter_v2"
REQUIRED_CHAINS = (
    "prerouting",
    "input",
    "output",
    "forward",
    "postrouting",
    "fwrouter_classify",
    "fwrouter_direct",
    "fwrouter_vpn",
    "fwrouter_vpn_full",
)

STATIC_SECURE_DNS_BYPASS_IPV4 = (
    "1.1.1.1",
    "1.0.0.1",
    "1.1.1.2",
    "1.0.0.2",
    "162.159.61.3",
    "162.159.61.4",
    "172.64.41.3",
    "172.64.41.4",
    "8.8.8.8",
    "8.8.4.4",
    "9.9.9.9",
    "149.112.112.112",
    "94.140.14.14",
    "94.140.15.15",
    "208.67.222.222",
    "208.67.220.220",
)


def _derive_tcp_redirect_mark_hex(vpn_fwmark_hex: str) -> str:
    return _derive_mark_hex(vpn_fwmark_hex, offset=1)


def _derive_mark_hex(vpn_fwmark_hex: str, *, offset: int) -> str:
    try:
        value = int(str(vpn_fwmark_hex), 16)
    except ValueError:
        value = 0x100
    return f"0x{value + offset:08x}"


def utc_timestamp() -> str:
    return datetime.now(timezone.utc).isoformat()


def _safe_set_suffix(value: str) -> str:
    return "".join(character.lower() if character.isalnum() else "_" for character in value).strip("_")


def _chunk_elements(elements: list[str], *, chunk_size: int = 512) -> list[list[str]]:
    return [elements[index : index + chunk_size] for index in range(0, len(elements), chunk_size)]


def _read_manifest_extra_ipv4_list(
    manifest: dict[str, Any] | None,
    key: str,
    *,
    default: tuple[str, ...] = (),
) -> list[str]:
    extra = manifest.get("extra") if isinstance(manifest, dict) else None
    values = extra.get(key) if isinstance(extra, dict) else None
    if not isinstance(values, list):
        return sorted(set(default))
    return sorted(
        {
            str(value).strip()
            for value in values
            if str(value).strip()
        }
    )


def _resolve_tproxy_handoff_ipv4(manifest: dict[str, Any] | None) -> str | None:
    global_preflight = manifest.get("global_preflight") if isinstance(manifest, dict) else None
    if not isinstance(global_preflight, dict):
        return None

    dnsmasq_status = global_preflight.get("dnsmasq_selective_status")
    if not isinstance(dnsmasq_status, dict):
        return None

    router_dns_ipv4 = dnsmasq_status.get("router_dns_ipv4")
    if isinstance(router_dns_ipv4, list):
        for candidate in router_dns_ipv4:
            value = str(candidate or "").strip()
            if value:
                return value

    dns_capture_status = dnsmasq_status.get("dns_capture_status")
    bindings = dns_capture_status.get("bindings") if isinstance(dns_capture_status, dict) else None
    if isinstance(bindings, list):
        for binding in bindings:
            if not isinstance(binding, dict):
                continue
            value = str(binding.get("address") or "").strip()
            if value:
                return value

    return None


def _resolve_lan_ingress_interfaces(manifest: dict[str, Any] | None) -> list[str]:
    global_preflight = manifest.get("global_preflight") if isinstance(manifest, dict) else None
    if not isinstance(global_preflight, dict):
        return []

    dnsmasq_status = global_preflight.get("dnsmasq_selective_status")
    if not isinstance(dnsmasq_status, dict):
        return []

    router_dns_interfaces = dnsmasq_status.get("router_dns_interfaces")
    if not isinstance(router_dns_interfaces, list):
        return []

    return sorted(
        {
            str(value).strip()
            for value in router_dns_interfaces
            if str(value).strip()
        }
    )


def _render_set(name: str, nft_type: str, elements: list[str]) -> tuple[list[str], list[str]]:
    if not elements:
        return [f"    set {name} {{ type {nft_type}; flags interval; auto-merge; }}"], []

    if len(elements) <= 256:
        lines = [
            f"    set {name} {{",
            f"        type {nft_type};",
            "        flags interval;",
            "        auto-merge;",
        ]
        lines.append("        elements = { " + ", ".join(elements) + " }")
        lines.append("    }")
        return lines, []

    lines = [
        f"    set {name} {{",
        f"        type {nft_type};",
        "        flags interval;",
        "        auto-merge;",
        "    }",
    ]
    add_commands = [f"flush set {OWNED_TABLE} {name}"]
    add_commands.extend(
        f"add element {OWNED_TABLE} {name} {{ " + ", ".join(chunk) + " }"
        for chunk in _chunk_elements(elements)
    )
    return lines, add_commands


def _render_dns_runtime_set(name: str, nft_type: str) -> list[str]:
    timeout_seconds = get_settings().dnsmasq_nftset_timeout_seconds
    return [
        f"    set {name} {{",
        f"        type {nft_type};",
        "        flags interval, timeout;",
        "        auto-merge;",
        f"        timeout {timeout_seconds}s;",
        "    }",
    ]


def _build_scoped_vpn_sets(manifest: dict[str, Any] | None) -> list[dict[str, Any]]:
    subjects = (manifest or {}).get("subjects") or []
    grouped: dict[str, dict[str, Any]] = {}

    for subject in subjects:
        if not isinstance(subject, dict):
            continue
        if subject.get("dataplane_path") != "vpn":
            continue
        scoped_runtime = subject.get("scoped_runtime")
        if not isinstance(scoped_runtime, dict):
            continue

        server_id = subject.get("selected_server_id") or "default"
        nft_type = str(scoped_runtime.get("nft_type") or "ipv4_addr")
        nft_expr = str(scoped_runtime.get("nft_expr") or "ip saddr")
        matcher_value = scoped_runtime.get("value")

        if not matcher_value:
            continue

        key = f"{server_id}:{nft_type}:{nft_expr}"
        if key not in grouped:
            grouped[key] = {
                "server_id": server_id,
                "nft_type": nft_type,
                "nft_expr": nft_expr,
                "values": [],
            }
        grouped[key]["values"].append(matcher_value)

    rendered: list[dict[str, Any]] = []
    for index, group in enumerate(grouped.values(), start=1):
        server_suffix = _safe_set_suffix(group["server_id"])[:24] or f"vpn_{index}"
        family_suffix = "v4" if group["nft_type"] == "ipv4_addr" else "v4" # We only do IPv4 for now
        rendered.append(
            {
                "set_name": f"scoped_{family_suffix}_{index}_{server_suffix}",
                "server_id": group["server_id"],
                "nft_type": group["nft_type"],
                "nft_expr": group["nft_expr"],
                "values": sorted(set(group["values"])),
            }
        )
    return rendered


def _resolve_rules_effective_artifact(manifest: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(manifest, dict):
        return read_effective_rules_artifact()

    extra = manifest.get("extra")
    manifest_rules = extra.get("rules_effective") if isinstance(extra, dict) else None
    if isinstance(manifest_rules, dict) and isinstance(manifest_rules.get("rules"), list):
        return manifest_rules

    return read_effective_rules_artifact()


def _build_classify_chain_lines(
    *,
    mode: str,
    selective_default: str,
    selective_vpn_ready: bool,
    selective_degraded: bool,
    scoped_steering_rules: list[str],
) -> list[str]:
    """Build the decision chain.

    `fwrouter_classify` decides only which terminal branch should handle the
    packet next. It must not perform the VPN mark/tproxy work itself, and it
    must not blur immunity/protected direct bypass with the terminal direct
    path semantics of `fwrouter_direct`.
    """

    classify_lines = [
        "    chain fwrouter_classify {",
        '        fib daddr type local goto fwrouter_direct comment "host-local destination always direct"',
        '        ip daddr @protected_ipv4 goto fwrouter_direct comment "protected IPv4 always direct"',
        '        ip6 daddr @protected_ipv6 goto fwrouter_direct comment "protected IPv6 always direct"',
        '        meta l4proto tcp tcp dport { 22 } goto fwrouter_direct comment "management tcp ingress direct"',
        *scoped_steering_rules,
    ]

    if mode == "direct":
        classify_lines.append('        goto fwrouter_direct comment "global direct v1"')
    elif mode == "vpn":
        classify_lines.append('        goto fwrouter_vpn_full comment "global vpn v1"')
    elif mode == "selective":
        classify_lines.extend(
            [
                '        ip daddr @direct_ipv4 goto fwrouter_direct comment "selective direct IPv4"',
                '        ip daddr @dns_direct_ipv4 goto fwrouter_direct comment "selective dns direct IPv4"',
                '        ip6 daddr @direct_ipv6 goto fwrouter_direct comment "selective direct IPv6"',
                (
                    '        ip daddr @vpn_ipv4 goto fwrouter_vpn comment "selective vpn IPv4"'
                    if selective_vpn_ready
                    else '        ip daddr @vpn_ipv4 drop comment "selective degraded block VPN IPv4"'
                ),
                (
                    '        ip daddr @dns_vpn_ipv4 goto fwrouter_vpn comment "selective dns vpn IPv4"'
                    if selective_vpn_ready
                    else '        ip daddr @dns_vpn_ipv4 drop comment "selective degraded block DNS VPN IPv4"'
                ),
                (
                    '        ip6 daddr @vpn_ipv6 goto fwrouter_vpn comment "selective vpn IPv6"'
                    if selective_vpn_ready
                    else '        ip6 daddr @vpn_ipv6 drop comment "selective degraded block VPN IPv6"'
                ),
            ]
        )
        target = "vpn_full" if selective_vpn_ready and selective_default == "vpn" else "direct"
        classify_comment = (
            f"selective default {selective_default}"
            if selective_vpn_ready and not selective_degraded
            else "selective degraded default direct"
        )
        classify_lines.append(f'        goto fwrouter_{target} comment "{classify_comment}"')

    classify_lines.append("    }")
    return classify_lines


def _build_terminal_direct_chain_lines(*, direct_counter_rules: list[str]) -> list[str]:
    """Build the terminal direct branch.

    `fwrouter_direct` is intentionally a terminal direct path. It can count and
    return/accept direct traffic, but it must not become a second classifier
    and must not grow VPN mark/tproxy logic.
    """

    return [
        "    chain fwrouter_direct {",
        *direct_counter_rules,
        '        counter return comment "global direct path"',
        "    }",
    ]


def _build_vpn_mark_chain_lines(
    *,
    vpn_tproxy_port: int | None,
    vpn_fwmark_hex: str,
) -> list[str]:
    vpn_mark_chain_lines = ["    chain fwrouter_vpn_mark {"]
    if isinstance(vpn_tproxy_port, int) and vpn_tproxy_port > 0:
        vpn_mark_chain_lines.extend(
            [
                f'        meta l4proto {{ tcp, udp }} meta mark set {vpn_fwmark_hex} counter comment "fwrouter vpn output fwmark v1"',
                '        counter return comment "fwrouter vpn output mark path"',
            ]
        )
    else:
        vpn_mark_chain_lines.append('        counter return comment "vpn output mark placeholder until Wave 2.2B+"')
    vpn_mark_chain_lines.append("    }")
    return vpn_mark_chain_lines


def _build_terminal_vpn_chain_lines(
    *,
    chain_name: str = "fwrouter_vpn",
    vpn_tproxy_port: int | None,
    vpn_redir_port: int | None,
    proxy_bypass_mark_hex: str,
    vpn_fwmark_hex: str,
    udp_fwmark_hex: str | None = None,
    tcp_redirect_mark_hex: str | None = None,
    vpn_counter_rules: list[str],
    vpn_policy_required: bool,
) -> list[str]:
    """Build the terminal VPN branch.

    `fwrouter_vpn` is where VPN-path packets are marked for the downstream
    tproxy contract. Classification must happen before this branch.
    """

    resolved_udp_fwmark_hex = udp_fwmark_hex or vpn_fwmark_hex
    resolved_tcp_redirect_mark_hex = tcp_redirect_mark_hex or _derive_tcp_redirect_mark_hex(vpn_fwmark_hex)
    vpn_chain_lines = [f"    chain {chain_name} {{"]
    if isinstance(vpn_tproxy_port, int) and vpn_tproxy_port > 0:
        contract_comment_suffix = (
            ' | fwrouter vpn policy contract required v1'
            if vpn_policy_required
            else ""
        )
        vpn_chain_lines.append(f'        meta mark {proxy_bypass_mark_hex} return comment "skip mihomo outbound recapture"')
        vpn_chain_lines.extend(vpn_counter_rules)
        vpn_chain_lines.extend(
            [
                '        meta l4proto udp udp dport 443 reject with icmpx type port-unreachable comment "force transparent web clients off QUIC onto TCP"',
                f'        meta l4proto tcp meta mark set {resolved_tcp_redirect_mark_hex} counter comment "fwrouter vpn mark tcp:{vpn_redir_port or vpn_tproxy_port}{contract_comment_suffix}"',
                f'        meta l4proto udp meta mark set {resolved_udp_fwmark_hex} counter comment "fwrouter vpn mark udp:{vpn_tproxy_port}{contract_comment_suffix}"',
                '        return comment "fwrouter global vpn mark path"',
            ]
        )
    else:
        vpn_chain_lines.extend(vpn_counter_rules)
        vpn_chain_lines.append('        counter return comment "vpn path placeholder until Wave 2.2B+"')
    vpn_chain_lines.append("    }")
    return vpn_chain_lines


def _build_prerouting_entry_chain_lines(
    *,
    proxy_bypass_mark_hex: str,
    vpn_fwmark_hex: str,
    full_vpn_udp_fwmark_hex: str,
    vpn_tproxy_port: int | None,
    full_vpn_tproxy_port: int | None,
    mode: str,
    vpn_counter_rules: list[str],
    vpn_policy_required: bool,
    lan_ingress_interfaces: list[str],
) -> list[str]:
    """Build the prerouting entrypoint.

    This is where immunity/bypass/protected ingress exclusions stay clearly
    ahead of subject/global classification and where the post-classify VPN
    tproxy contract is terminated.
    """
    udp_tproxy_target = f"tproxy to :{vpn_tproxy_port}"
    full_udp_tproxy_target = f"tproxy to :{full_vpn_tproxy_port}"

    prerouting_chain_lines = [
        "    chain prerouting {",
        "        type filter hook prerouting priority mangle; policy accept;",
        '        socket transparent 1 accept comment "immunity: established tproxy sessions"',
        f'        meta mark {proxy_bypass_mark_hex} accept comment "immunity: mihomo outbound bypass"',
        '        iifname "tailscale0" accept comment "immunity: tailscale ingress"',
        '        ip saddr @infrastructure_ipv4 accept comment "immunity: infrastructure outbound"',
        '        udp sport 68 udp dport 67 accept comment "immunity: DHCP client requests to dnsmasq"',
        '        udp sport 67 udp dport 68 accept comment "immunity: DHCP server replies"',
        *[
            f'        iifname "{interface}" meta nfproto ipv6 reject with icmpv6 type admin-prohibited comment "block IPv6 from LAN ingress {interface}"'
            for interface in lan_ingress_interfaces
        ],
        *[
            f'        iifname "{interface}" meta l4proto {{ tcp, udp }} th dport 53 accept comment "allow LAN DNS capture before VPN classify {interface}"'
            for interface in lan_ingress_interfaces
        ],
        '        ip saddr { 10.0.0.0/8, 100.64.0.0/10, 172.16.0.0/12, 192.168.0.0/16 } ip daddr @secure_dns_bypass_ipv4 meta l4proto tcp tcp dport { 443, 853 } reject with tcp reset comment "reject secure DNS bypass TCP from LAN"',
        '        ip saddr { 10.0.0.0/8, 100.64.0.0/10, 172.16.0.0/12, 192.168.0.0/16 } ip daddr @secure_dns_bypass_ipv4 meta l4proto udp udp dport { 443, 853 } reject with icmpx type port-unreachable comment "reject secure DNS bypass UDP from LAN"',
        '        jump fwrouter_classify comment "FWRouter global classify"',
    ]
    if (mode in {"vpn", "selective"} or vpn_counter_rules) and isinstance(vpn_tproxy_port, int) and vpn_tproxy_port > 0:
        contract_comment_suffix = (
            ' | fwrouter vpn policy contract required v1'
            if vpn_policy_required
            else ""
        )
        prerouting_chain_lines.extend(
            [
                f'        meta mark {vpn_fwmark_hex} meta l4proto udp counter {udp_tproxy_target} accept comment "fwrouter tproxy handoff udp:{vpn_tproxy_port}{contract_comment_suffix}"',
            ]
        )
    if (
        (mode in {"vpn", "selective"} or vpn_counter_rules)
        and isinstance(full_vpn_tproxy_port, int)
        and full_vpn_tproxy_port > 0
    ):
        contract_comment_suffix = (
            ' | fwrouter vpn policy contract required v1'
            if vpn_policy_required
            else ""
        )
        prerouting_chain_lines.append(
            f'        meta mark {full_vpn_udp_fwmark_hex} meta l4proto udp counter {full_udp_tproxy_target} accept comment "fwrouter full-vpn tproxy handoff udp:{full_vpn_tproxy_port}{contract_comment_suffix}"'
        )
    prerouting_chain_lines.append("    }")
    return prerouting_chain_lines


def _build_prerouting_nat_chain_lines(
    *,
    vpn_fwmark_hex: str,
    vpn_redir_port: int | None,
    full_vpn_tcp_redirect_mark_hex: str | None = None,
    full_vpn_redir_port: int | None = None,
    vpn_policy_required: bool,
) -> list[str]:
    prerouting_nat_chain_lines = [
        "    chain prerouting_nat {",
        "        type nat hook prerouting priority dstnat; policy accept;",
    ]
    if isinstance(vpn_redir_port, int) and vpn_redir_port > 0:
        tcp_redirect_mark_hex = _derive_tcp_redirect_mark_hex(vpn_fwmark_hex)
        contract_comment_suffix = (
            ' | fwrouter vpn policy contract required v1'
            if vpn_policy_required
            else ""
        )
        prerouting_nat_chain_lines.append(
            f'        meta mark {tcp_redirect_mark_hex} meta l4proto tcp counter redirect to :{vpn_redir_port} comment "fwrouter redirect handoff tcp:{vpn_redir_port}{contract_comment_suffix}"'
        )
    if isinstance(full_vpn_redir_port, int) and full_vpn_redir_port > 0:
        full_tcp_mark = full_vpn_tcp_redirect_mark_hex or _derive_mark_hex(vpn_fwmark_hex, offset=3)
        contract_comment_suffix = (
            ' | fwrouter vpn policy contract required v1'
            if vpn_policy_required
            else ""
        )
        prerouting_nat_chain_lines.append(
            f'        meta mark {full_tcp_mark} meta l4proto tcp counter redirect to :{full_vpn_redir_port} comment "fwrouter full-vpn redirect handoff tcp:{full_vpn_redir_port}{contract_comment_suffix}"'
        )
    prerouting_nat_chain_lines.append("    }")
    return prerouting_nat_chain_lines


def _build_output_entry_chain_lines(
    *,
    vpn_rx_counter_rules: list[str],
    proxy_bypass_mark_hex: str,
    system_output_steering_rules: list[str],
    mode: str,
) -> list[str]:
    """Build the output entrypoint.

    Output hook keeps host-local/protected/management immunity ahead of any
    subject/global classification. The final unconditional direct fallback here
    is an output-entry decision, not terminal direct-chain behavior.
    """

    output_lines = [
        "    chain output {",
        "        type route hook output priority mangle; policy accept;",
        *vpn_rx_counter_rules,
        '        oifname "tailscale0" accept comment "immunity: tailscale egress"',
        f'        meta mark {proxy_bypass_mark_hex} return comment "skip mihomo outbound recapture"',
        '        fib daddr type local goto fwrouter_direct comment "host output to local destination always direct"',
        '        ip daddr @protected_ipv4 goto fwrouter_direct comment "host output to protected IPv4 always direct"',
        '        ip6 daddr @protected_ipv6 goto fwrouter_direct comment "host output to protected IPv6 always direct"',
        '        meta l4proto tcp tcp sport { 22 } goto fwrouter_direct comment "management tcp output direct"',
        '        meta l4proto tcp tcp dport { 22 } goto fwrouter_direct comment "management tcp output direct"',
        *system_output_steering_rules,
    ]
    comment_mode = "global vpn" if mode == "vpn" else "selective" if mode == "selective" else "global direct"
    output_lines.append(
        f'        goto fwrouter_direct comment "host output stays direct in {comment_mode} mode"'
    )
    output_lines.append("    }")
    return output_lines


def _build_output_nat_chain_lines(
    *,
    vpn_fwmark_hex: str,
    vpn_redir_port: int | None,
    full_vpn_tcp_redirect_mark_hex: str | None = None,
    full_vpn_redir_port: int | None = None,
    proxy_bypass_mark_hex: str,
    vpn_policy_required: bool,
) -> list[str]:
    output_nat_lines = [
        "    chain output_nat {",
        "        type nat hook output priority -100; policy accept;",
        f'        meta mark {proxy_bypass_mark_hex} return comment "skip mihomo outbound recapture"',
    ]
    if isinstance(vpn_redir_port, int) and vpn_redir_port > 0:
        contract_comment_suffix = (
            ' | fwrouter vpn policy contract required v1'
            if vpn_policy_required
            else ""
        )
        output_nat_lines.append(
            f'        meta mark {vpn_fwmark_hex} meta l4proto tcp counter redirect to :{vpn_redir_port} comment "fwrouter redirect handoff tcp:{vpn_redir_port}{contract_comment_suffix}"'
        )
    if isinstance(full_vpn_redir_port, int) and full_vpn_redir_port > 0:
        full_tcp_mark = full_vpn_tcp_redirect_mark_hex or _derive_mark_hex(vpn_fwmark_hex, offset=3)
        contract_comment_suffix = (
            ' | fwrouter vpn policy contract required v1'
            if vpn_policy_required
            else ""
        )
        output_nat_lines.append(
            f'        meta mark {full_tcp_mark} meta l4proto tcp counter redirect to :{full_vpn_redir_port} comment "fwrouter full-vpn redirect handoff tcp:{full_vpn_redir_port}{contract_comment_suffix}"'
        )
    output_nat_lines.append("    }")
    return output_nat_lines


def render_owned_table_candidate(manifest: dict[str, Any] | None = None) -> str:
    core_bypass = (
        ((manifest or {}).get("extra") or {}).get("core_bypass")
        if isinstance(manifest, dict)
        else None
    )
    if isinstance(core_bypass, dict) and core_bypass.get("enabled"):
        lines = [
            f"table {OWNED_TABLE} {{",
            '    comment "FWRouter v2 owned table - intentional bypass/direct-safe mode"',
            "",
            "    chain prerouting {",
            "        type filter hook prerouting priority mangle; policy accept;",
            '        counter return comment "fwrouter core bypass prerouting"',
            "    }",
            "",
            "    chain input {",
            "        type filter hook input priority filter; policy accept;",
            '        counter return comment "fwrouter core bypass input"',
            "    }",
            "",
            "    chain output {",
            "        type route hook output priority mangle; policy accept;",
            '        counter return comment "fwrouter core bypass output"',
            "    }",
            "",
            "    chain forward {",
            "        type filter hook forward priority filter; policy accept;",
            '        counter return comment "fwrouter core bypass forward"',
            "    }",
            "",
            "    chain postrouting {",
            "        type filter hook postrouting priority filter; policy accept;",
            '        counter return comment "fwrouter core bypass postrouting"',
            "    }",
            "",
            "    chain fwrouter_classify {",
            '        counter return comment "fwrouter core bypass classify"',
            "    }",
            "",
            "    chain fwrouter_direct {",
            '        counter return comment "fwrouter core bypass direct-safe"',
            "    }",
            "",
            "    chain fwrouter_vpn {",
            '        counter return comment "fwrouter core bypass vpn disabled"',
            "    }",
            "",
            "    chain fwrouter_vpn_full {",
            '        counter return comment "fwrouter core bypass full vpn disabled"',
            "    }",
            "}",
            "",
        ]
        return "\n".join(lines)

    summary = manifest.get("summary") if isinstance(manifest, dict) else {}
    mode = str(summary.get("global_mode") or "direct")
    selective_default = str(summary.get("selective_default") or "direct").lower()
    vpn_policy_required = bool(summary.get("requires_vpn_policy_routing"))

    preflight = manifest.get("global_preflight") if isinstance(manifest, dict) else {}
    vpn_contour = preflight.get("vpn_contour") if isinstance(preflight, dict) else {}
    profile = preflight.get("profile") if isinstance(preflight, dict) else {}
    mihomo_profile = profile.get("mihomo") if isinstance(profile, dict) else {}
    contour_profile = mihomo_profile.get("contours") if isinstance(mihomo_profile, dict) else {}

    rules_effective = _resolve_rules_effective_artifact(manifest)
    nft_sets = build_nft_rule_sets(rules_effective if isinstance(rules_effective, dict) else None)

    vpn_redir_port = vpn_contour.get("redir_port") if isinstance(vpn_contour, dict) else None
    if not isinstance(vpn_redir_port, int) and isinstance(vpn_contour, dict) and vpn_contour.get("tproxy_port"):
        vpn_redir_port = 5202
    vpn_tproxy_port = vpn_contour.get("tproxy_port") if isinstance(vpn_contour, dict) else None
    full_vpn_redir_port = vpn_contour.get("full_vpn_redir_port") if isinstance(vpn_contour, dict) else None
    if not isinstance(full_vpn_redir_port, int):
        full_vpn_redir_port = 5204
    full_vpn_tproxy_port = vpn_contour.get("full_vpn_tproxy_port") if isinstance(vpn_contour, dict) else None
    if not isinstance(full_vpn_tproxy_port, int):
        full_vpn_tproxy_port = 5205
    vpn_fwmark_hex = str(vpn_contour.get("fwmark_hex") or "0x00000100") if isinstance(vpn_contour, dict) else "0x00000100"
    full_vpn_udp_fwmark_hex = _derive_mark_hex(vpn_fwmark_hex, offset=2)
    full_vpn_tcp_redirect_mark_hex = _derive_mark_hex(vpn_fwmark_hex, offset=3)
    proxy_bypass_mark_hex = (
        str(vpn_contour.get("proxy_bypass_mark_hex") or "0x00000200")
        if isinstance(vpn_contour, dict)
        else "0x00000200"
    )
    selective_path_kind = str(contour_profile.get("selective_path_kind") or "ip_only").strip().lower()
    selective_vpn_ready = bool(preflight.get("selective_vpn_ready", False))
    selective_degraded = bool(preflight.get("selective_degraded", False))
    scoped_vpn_sets = _build_scoped_vpn_sets(manifest)
    lan_ingress_interfaces = _resolve_lan_ingress_interfaces(manifest)
    if not vpn_policy_required and isinstance(preflight, dict):
        vpn_policy_required = bool(preflight.get("vpn_policy_required", False))

    subjects = (manifest.get("subjects") or []) if isinstance(manifest, dict) else []
    
    infrastructure_ips = []
    for s in subjects:
        if not isinstance(s, dict):
            continue
        scoped_runtime = s.get("scoped_runtime")
        matcher = scoped_runtime.get("matcher") if isinstance(scoped_runtime, dict) else None
        if (
            s.get("subject_type") in {"docker", "host", "fwrouter"}
            and isinstance(matcher, dict)
            and matcher.get("family") == "ipv4"
            and matcher.get("value")
        ):
            infrastructure_ips.append(str(matcher["value"]))

    infrastructure_ips.extend(
        _read_manifest_extra_ipv4_list(manifest, "infrastructure_ipv4")
    )
    infrastructure_ips = sorted(set(infrastructure_ips))
    secure_dns_bypass_ipv4 = _read_manifest_extra_ipv4_list(
        manifest,
        "secure_dns_bypass_ipv4",
        default=STATIC_SECURE_DNS_BYPASS_IPV4,
    )

    active_subjects = [s for s in subjects if isinstance(s, dict) and s.get("is_active")]

    if not vpn_policy_required:
        selective_rules = preflight.get("selective_rules") if isinstance(preflight, dict) else {}
        selective_requires_vpn_runtime = bool(
            isinstance(selective_rules, dict) and selective_rules.get("requires_vpn_runtime")
        )
        selective_reaches_vpn = selective_vpn_ready and (
            selective_requires_vpn_runtime or selective_default == "vpn"
        )
        if mode == "vpn" or (mode == "selective" and selective_reaches_vpn):
            vpn_policy_required = True
        else:
            for subject in active_subjects:
                if str(subject.get("subject_type") or "").strip().lower() == "xray":
                    continue
                path = str(subject.get("dataplane_path") or "").strip().lower()
                if path == "vpn":
                    vpn_policy_required = True
                    break
                if path == "selective" and selective_reaches_vpn:
                    vpn_policy_required = True
                    break

    def _resolved_subject_path(subject: dict[str, Any]) -> str:
        explicit_path = str(subject.get("dataplane_path") or "").strip().lower()
        if explicit_path:
            return explicit_path
        effective_state = subject.get("effective_state")
        if isinstance(effective_state, dict):
            effective_path = str(effective_state.get("dataplane_path") or "").strip().lower()
            if effective_path:
                return effective_path
            effective_mode = str(effective_state.get("effective_mode") or "").strip().lower()
            if effective_mode in {"direct", "vpn", "selective"}:
                return effective_mode
        return ""

    def _scoped_matcher(subject: dict[str, Any]) -> tuple[str | None, str | None, str | None]:
        scoped = subject.get("scoped_runtime")
        if not isinstance(scoped, dict):
            return None, None, None

        matcher = scoped.get("matcher")
        if not isinstance(matcher, dict):
            matcher = scoped

        expr = matcher.get("nft_expr")
        val = matcher.get("value")
        family = matcher.get("family")
        if not expr or not val:
            return None, None, None

        return str(expr), str(val), str(family or "")

    counter_declarations: list[str] = []
    direct_counter_rules: list[str] = []
    vpn_counter_rules: list[str] = []
    direct_rx_counter_rules: list[str] = []
    vpn_rx_counter_rules: list[str] = []

    for subject in active_subjects:
        sid = subject.get("subject_id")
        slug = _safe_set_suffix(sid)
        counter_declarations.append(f'    counter cnt_{slug}_direct_tx {{ }}')
        counter_declarations.append(f'    counter cnt_{slug}_direct_rx {{ }}')
        counter_declarations.append(f'    counter cnt_{slug}_vpn_tx {{ }}')
        counter_declarations.append(f'    counter cnt_{slug}_vpn_rx {{ }}')

        expr, val, _family = _scoped_matcher(subject)
        if expr and val:
            direct_counter_rules.append(f'        {expr} {val} counter name "cnt_{slug}_direct_tx"')
            vpn_counter_rules.append(f'        {expr} {val} counter name "cnt_{slug}_vpn_tx"')
            
            rx_expr = None
            if expr == "ip saddr":
                rx_expr = "ip daddr"
            elif expr == "ip6 saddr":
                rx_expr = "ip6 daddr"
            
            if rx_expr:
                direct_rx_counter_rules.append(f'        {rx_expr} {val} counter name "cnt_{slug}_direct_rx"')
                vpn_rx_counter_rules.append(
                    f'        meta mark {proxy_bypass_mark_hex} {rx_expr} {val} counter name "cnt_{slug}_vpn_rx"'
                )

    scoped_steering_rules: list[str] = []
    system_output_steering_rules: list[str] = []
    for subject in active_subjects:
        path = _resolved_subject_path(subject)
        expr, val, family = _scoped_matcher(subject)
        if not expr or not val:
            continue
        subject_type = str(subject.get("subject_type") or "")

        if path == "selective":
            if family == "ipv4":
                scoped_steering_rules.extend(
                    [
                        f'        {expr} {val} ip daddr @direct_ipv4 goto fwrouter_direct comment "scoped selective direct IPv4: {subject.get("subject_id")}"',
                        f'        {expr} {val} ip daddr @dns_direct_ipv4 goto fwrouter_direct comment "scoped selective dns direct IPv4: {subject.get("subject_id")}"',
                        (
                            f'        {expr} {val} ip daddr @vpn_ipv4 goto fwrouter_vpn comment "scoped selective vpn IPv4: {subject.get("subject_id")}"'
                            if selective_vpn_ready
                            else f'        {expr} {val} ip daddr @vpn_ipv4 drop comment "scoped selective degraded block VPN IPv4: {subject.get("subject_id")}"'
                        ),
                        (
                            f'        {expr} {val} ip daddr @dns_vpn_ipv4 goto fwrouter_vpn comment "scoped selective dns vpn IPv4: {subject.get("subject_id")}"'
                            if selective_vpn_ready
                            else f'        {expr} {val} ip daddr @dns_vpn_ipv4 drop comment "scoped selective degraded block DNS VPN IPv4: {subject.get("subject_id")}"'
                        ),
                    ]
                )
            elif family == "ipv6":
                scoped_steering_rules.extend(
                    [
                        f'        {expr} {val} ip6 daddr @direct_ipv6 goto fwrouter_direct comment "scoped selective direct IPv6: {subject.get("subject_id")}"',
                        (
                            f'        {expr} {val} ip6 daddr @vpn_ipv6 goto fwrouter_vpn comment "scoped selective vpn IPv6: {subject.get("subject_id")}"'
                            if selective_vpn_ready
                            else f'        {expr} {val} ip6 daddr @vpn_ipv6 drop comment "scoped selective degraded block VPN IPv6: {subject.get("subject_id")}"'
                        ),
                    ]
                )

            target = "vpn" if selective_vpn_ready and selective_default == "vpn" else "direct"
            comment = (
                f"scoped selective default {selective_default}: {subject.get('subject_id')}"
                if selective_vpn_ready
                else f"scoped selective degraded default direct: {subject.get('subject_id')}"
            )
            scoped_steering_rules.append(
                f'        {expr} {val} goto fwrouter_{target} comment "{comment}"'
            )
            continue

        if path in {"vpn", "direct"}:
            target = "vpn_full" if path == "vpn" else "direct"
            scoped_steering_rules.append(
                f'        {expr} {val} goto fwrouter_{target} comment "scoped {path} override: {subject.get("subject_id")}"'
            )
            if subject_type in {"host", "docker"} and path == "vpn":
                system_output_steering_rules.append(
                    f'        {expr} {val} goto fwrouter_{target} comment "system scoped {path} output override: {subject.get("subject_id")}"'
                )

    classify_lines = _build_classify_chain_lines(
        mode=mode,
        selective_default=selective_default,
        selective_vpn_ready=selective_vpn_ready,
        selective_degraded=selective_degraded,
        scoped_steering_rules=scoped_steering_rules,
    )
    vpn_mark_chain_lines = _build_vpn_mark_chain_lines(
        vpn_tproxy_port=vpn_tproxy_port,
        vpn_fwmark_hex=vpn_fwmark_hex,
    )
    vpn_chain_lines = _build_terminal_vpn_chain_lines(
        chain_name="fwrouter_vpn",
        vpn_tproxy_port=vpn_tproxy_port,
        vpn_redir_port=vpn_redir_port,
        proxy_bypass_mark_hex=proxy_bypass_mark_hex,
        vpn_fwmark_hex=vpn_fwmark_hex,
        vpn_counter_rules=vpn_counter_rules,
        vpn_policy_required=vpn_policy_required,
    )
    full_vpn_chain_lines = _build_terminal_vpn_chain_lines(
        chain_name="fwrouter_vpn_full",
        vpn_tproxy_port=full_vpn_tproxy_port,
        vpn_redir_port=full_vpn_redir_port,
        proxy_bypass_mark_hex=proxy_bypass_mark_hex,
        vpn_fwmark_hex=vpn_fwmark_hex,
        udp_fwmark_hex=full_vpn_udp_fwmark_hex,
        tcp_redirect_mark_hex=full_vpn_tcp_redirect_mark_hex,
        vpn_counter_rules=vpn_counter_rules,
        vpn_policy_required=vpn_policy_required,
    )
    prerouting_chain_lines = _build_prerouting_entry_chain_lines(
        proxy_bypass_mark_hex=proxy_bypass_mark_hex,
        vpn_fwmark_hex=vpn_fwmark_hex,
        full_vpn_udp_fwmark_hex=full_vpn_udp_fwmark_hex,
        vpn_tproxy_port=vpn_tproxy_port,
        full_vpn_tproxy_port=full_vpn_tproxy_port,
        mode=mode,
        vpn_counter_rules=vpn_counter_rules,
        vpn_policy_required=vpn_policy_required,
        lan_ingress_interfaces=lan_ingress_interfaces,
    )
    prerouting_nat_chain_lines = _build_prerouting_nat_chain_lines(
        vpn_fwmark_hex=vpn_fwmark_hex,
        vpn_redir_port=vpn_redir_port,
        full_vpn_tcp_redirect_mark_hex=full_vpn_tcp_redirect_mark_hex,
        full_vpn_redir_port=full_vpn_redir_port,
        vpn_policy_required=vpn_policy_required,
    )

    tproxy_input_guard_lines = [
        "    chain input {",
        "        type filter hook input priority filter; policy accept;",
    ]
    if isinstance(vpn_tproxy_port, int) and vpn_tproxy_port > 0:
        tproxy_input_guard_lines.extend(
            [
                f'        iifname "lo" meta l4proto {{ tcp, udp }} th dport {vpn_tproxy_port} accept comment "allow local fwrouter tproxy access"',
                f'        ip saddr {{ 10.0.0.0/8, 100.64.0.0/10, 172.16.0.0/12, 192.168.0.0/16 }} meta l4proto {{ tcp, udp }} th dport {vpn_tproxy_port} accept comment "allow trusted IPv4 fwrouter tproxy access"',
                f'        ip6 saddr {{ fc00::/7, fe80::/10 }} meta l4proto {{ tcp, udp }} th dport {vpn_tproxy_port} accept comment "allow trusted IPv6 fwrouter tproxy access"',
                f'        meta l4proto {{ tcp, udp }} th dport {vpn_tproxy_port} drop comment "block public access to fwrouter tproxy"',
            ]
        )
    if isinstance(vpn_redir_port, int) and vpn_redir_port > 0:
        tproxy_input_guard_lines.extend(
            [
                f'        iifname "lo" meta l4proto tcp th dport {vpn_redir_port} accept comment "allow local fwrouter redir access"',
                f'        ip saddr {{ 10.0.0.0/8, 100.64.0.0/10, 172.16.0.0/12, 192.168.0.0/16 }} meta l4proto tcp th dport {vpn_redir_port} accept comment "allow trusted IPv4 fwrouter redir access"',
                f'        ip6 saddr {{ fc00::/7, fe80::/10 }} meta l4proto tcp th dport {vpn_redir_port} accept comment "allow trusted IPv6 fwrouter redir access"',
                f'        meta l4proto tcp th dport {vpn_redir_port} drop comment "block public access to fwrouter redir"',
            ]
        )
    if isinstance(full_vpn_tproxy_port, int) and full_vpn_tproxy_port > 0:
        tproxy_input_guard_lines.extend(
            [
                f'        iifname "lo" meta l4proto {{ tcp, udp }} th dport {full_vpn_tproxy_port} accept comment "allow local fwrouter full-vpn tproxy access"',
                f'        ip saddr {{ 10.0.0.0/8, 100.64.0.0/10, 172.16.0.0/12, 192.168.0.0/16 }} meta l4proto {{ tcp, udp }} th dport {full_vpn_tproxy_port} accept comment "allow trusted IPv4 fwrouter full-vpn tproxy access"',
                f'        ip6 saddr {{ fc00::/7, fe80::/10 }} meta l4proto {{ tcp, udp }} th dport {full_vpn_tproxy_port} accept comment "allow trusted IPv6 fwrouter full-vpn tproxy access"',
                f'        meta l4proto {{ tcp, udp }} th dport {full_vpn_tproxy_port} drop comment "block public access to fwrouter full-vpn tproxy"',
            ]
        )
    if isinstance(full_vpn_redir_port, int) and full_vpn_redir_port > 0:
        tproxy_input_guard_lines.extend(
            [
                f'        iifname "lo" meta l4proto tcp th dport {full_vpn_redir_port} accept comment "allow local fwrouter full-vpn redir access"',
                f'        ip saddr {{ 10.0.0.0/8, 100.64.0.0/10, 172.16.0.0/12, 192.168.0.0/16 }} meta l4proto tcp th dport {full_vpn_redir_port} accept comment "allow trusted IPv4 fwrouter full-vpn redir access"',
                f'        ip6 saddr {{ fc00::/7, fe80::/10 }} meta l4proto tcp th dport {full_vpn_redir_port} accept comment "allow trusted IPv6 fwrouter full-vpn redir access"',
                f'        meta l4proto tcp th dport {full_vpn_redir_port} drop comment "block public access to fwrouter full-vpn redir"',
            ]
        )
    tproxy_input_guard_lines.append("    }")

    lines = [
        f"table {OWNED_TABLE} {{",
        '    comment "FWRouter v2 owned table - managed only by FWRouter"',
        "",
        *counter_declarations,
        "",
    ]

    deferred_element_commands: list[str] = []

    set_definitions, set_commands = _render_set("protected_ipv4", "ipv4_addr", nft_sets["protected_ipv4"])
    lines.extend(set_definitions)
    deferred_element_commands.extend(set_commands)
    lines.extend([
        "",
    ])

    set_definitions, set_commands = _render_set("protected_ipv6", "ipv6_addr", nft_sets["protected_ipv6"])
    lines.extend(set_definitions)
    deferred_element_commands.extend(set_commands)
    lines.extend([
        "",
    ])

    set_definitions, set_commands = _render_set("infrastructure_ipv4", "ipv4_addr", infrastructure_ips)
    lines.extend(set_definitions)
    deferred_element_commands.extend(set_commands)
    lines.extend([
        "",
    ])

    set_definitions, set_commands = _render_set("secure_dns_bypass_ipv4", "ipv4_addr", secure_dns_bypass_ipv4)
    lines.extend(set_definitions)
    deferred_element_commands.extend(set_commands)
    lines.extend([
        "",
    ])

    set_definitions, set_commands = _render_set("direct_ipv4", "ipv4_addr", nft_sets["direct_ipv4"])
    lines.extend(set_definitions)
    deferred_element_commands.extend(set_commands)
    lines.extend([
        "",
    ])

    lines.extend(_render_dns_runtime_set("dns_direct_ipv4", "ipv4_addr"))
    lines.extend([
        "",
    ])

    set_definitions, set_commands = _render_set("direct_ipv6", "ipv6_addr", nft_sets["direct_ipv6"])
    lines.extend(set_definitions)
    deferred_element_commands.extend(set_commands)
    lines.extend([
        "",
    ])

    set_definitions, set_commands = _render_set("vpn_ipv4", "ipv4_addr", nft_sets["vpn_ipv4"])
    lines.extend(set_definitions)
    deferred_element_commands.extend(set_commands)
    lines.extend([
        "",
    ])

    lines.extend(_render_dns_runtime_set("dns_vpn_ipv4", "ipv4_addr"))
    lines.extend([
        "",
    ])

    set_definitions, set_commands = _render_set("vpn_ipv6", "ipv6_addr", nft_sets["vpn_ipv6"])
    lines.extend(set_definitions)
    deferred_element_commands.extend(set_commands)
    lines.extend([
        "",
    ])

    for s_set in scoped_vpn_sets:
        set_definitions, set_commands = _render_set(s_set["set_name"], s_set["nft_type"], s_set["values"])
        lines.extend(set_definitions)
        deferred_element_commands.extend(set_commands)
        lines.append("")

    lines.extend(
        [
            *prerouting_chain_lines,
            "",
            *prerouting_nat_chain_lines,
            "",
            *tproxy_input_guard_lines,
            "",
            *_build_output_entry_chain_lines(
                vpn_rx_counter_rules=vpn_rx_counter_rules,
                proxy_bypass_mark_hex=proxy_bypass_mark_hex,
                system_output_steering_rules=system_output_steering_rules,
                mode=mode,
            ),
            "",
            *_build_output_nat_chain_lines(
                vpn_fwmark_hex=vpn_fwmark_hex,
                vpn_redir_port=vpn_redir_port,
                full_vpn_tcp_redirect_mark_hex=full_vpn_tcp_redirect_mark_hex,
                full_vpn_redir_port=full_vpn_redir_port,
                proxy_bypass_mark_hex=proxy_bypass_mark_hex,
                vpn_policy_required=vpn_policy_required,
            ),
            "",
            "    chain forward {",
            "        type filter hook forward priority filter; policy accept;",
            *direct_rx_counter_rules,
            '        counter comment "fwrouter_v2 forward global v1"',
            "    }",
            "",
            "    chain postrouting {",
            "        type filter hook postrouting priority filter; policy accept;",
            '        tcp flags syn tcp option maxseg size set rt mtu comment "TCP MSS Clamping for VPN reliability"',
            '        counter comment "fwrouter_v2 postrouting global v1"',
            "    }",
            "",
            *classify_lines,
            "",
            *_build_terminal_direct_chain_lines(
                direct_counter_rules=direct_counter_rules,
            ),
            "",
            *vpn_mark_chain_lines,
            "",
            *vpn_chain_lines,
            "",
            *full_vpn_chain_lines,
            "}",
            "",
        ]
    )
    if deferred_element_commands:
        lines.extend(deferred_element_commands)
        lines.append("")
    return "\n".join(lines)


def get_dataplane_artifact_paths(*, job_id: str, apply_id: str) -> dict[str, str]:
    paths = get_settings().paths
    generated_dir = paths.generated_dir / "dataplane"
    last_good_dir = paths.state_dir / "last-good" / "dataplane"
    snapshot_dir = last_good_dir / "snapshots" / apply_id
    job_dir = paths.jobs_dir / job_id / "dataplane"

    return {
        "generated_dir": str(generated_dir),
        "last_good_dir": str(last_good_dir),
        "snapshot_dir": str(snapshot_dir),
        "job_dataplane_dir": str(job_dir),
        "candidate_nft_path": str(generated_dir / "candidate.nft"),
        "candidate_manifest_path": str(generated_dir / "candidate-manifest.json"),
        "current_nft_path": str(generated_dir / "current.nft"),
        "current_manifest_path": str(generated_dir / "current-manifest.json"),
        "applied_nft_path": str(generated_dir / "applied.nft"),
        "applied_manifest_path": str(generated_dir / "applied-manifest.json"),
        "last_good_nft_path": str(last_good_dir / "last-good.nft"),
        "last_good_manifest_path": str(last_good_dir / "last-good-manifest.json"),
        "snapshot_before_nft_path": str(snapshot_dir / "fwrouter_v2.before.nft"),
        "snapshot_state_path": str(snapshot_dir / "snapshot-state.json"),
        "snapshot_candidate_nft_path": str(snapshot_dir / "candidate.nft"),
        "snapshot_manifest_path": str(snapshot_dir / "manifest.json"),
        "job_candidate_nft_path": str(job_dir / "candidate.nft"),
        "job_candidate_manifest_path": str(job_dir / "candidate-manifest.json"),
        "job_result_path": str(job_dir / "result.json"),
        "job_check_stdout_path": str(job_dir / "check.stdout"),
        "job_check_stderr_path": str(job_dir / "check.stderr"),
        "job_apply_stdout_path": str(job_dir / "apply.stdout"),
        "job_apply_stderr_path": str(job_dir / "apply.stderr"),
        "job_rollback_stdout_path": str(job_dir / "rollback.stdout"),
        "job_rollback_stderr_path": str(job_dir / "rollback.stderr"),
    }


def write_candidate_artifacts(
    *,
    job_id: str,
    apply_id: str,
    manifest: dict[str, Any],
) -> dict[str, str]:
    artifact_paths = get_dataplane_artifact_paths(job_id=job_id, apply_id=apply_id)
    candidate_text = render_owned_table_candidate(manifest)

    atomic_write_text(Path(artifact_paths["candidate_nft_path"]), candidate_text)
    atomic_write_text(Path(artifact_paths["job_candidate_nft_path"]), candidate_text)
    atomic_write_text(Path(artifact_paths["snapshot_candidate_nft_path"]), candidate_text)

    candidate_manifest_path = Path(artifact_paths["candidate_manifest_path"])
    atomic_write_json(candidate_manifest_path, manifest)
    atomic_copy_file(candidate_manifest_path, Path(artifact_paths["job_candidate_manifest_path"]))
    atomic_copy_file(candidate_manifest_path, Path(artifact_paths["snapshot_manifest_path"]))

    return artifact_paths


def promote_last_good(
    *,
    manifest: dict[str, Any],
    artifact_paths: dict[str, str],
) -> None:
    candidate_text = Path(artifact_paths["candidate_nft_path"]).read_text(encoding="utf-8")
    atomic_write_text(Path(artifact_paths["current_nft_path"]), candidate_text)
    atomic_write_text(Path(artifact_paths["applied_nft_path"]), candidate_text)
    atomic_write_text(Path(artifact_paths["last_good_nft_path"]), candidate_text)
    applied_manifest_path = Path(artifact_paths["applied_manifest_path"])
    atomic_write_json(applied_manifest_path, manifest)
    atomic_copy_file(applied_manifest_path, Path(artifact_paths["last_good_manifest_path"]))

    current_manifest_path = artifact_paths.get("current_manifest_path")
    if current_manifest_path:
        atomic_copy_file(applied_manifest_path, Path(current_manifest_path))
