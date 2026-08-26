from __future__ import annotations

from typing import Any

from fwrouter_api.services.dataplane_nft_constants import (
    CONTROL_PLANE_INPUT_PORTS,
    ROOT_UID,
    _derive_mark_hex,
    _derive_tcp_redirect_mark_hex,
)
from fwrouter_api.services.subject_taxonomy import external_ingress_contracts


def _nft_port_match_value(ports: list[int | None]) -> str | None:
    unique_ports = sorted({port for port in ports if isinstance(port, int) and port > 0})
    if not unique_ports:
        return None
    if len(unique_ports) == 1:
        return str(unique_ports[0])
    return "{ " + ", ".join(str(port) for port in unique_ports) + " }"


def _external_ingress_immunity_lines() -> list[str]:
    lines: list[str] = []
    for contract in external_ingress_contracts():
        interface = str(contract.get("ingress_interface") or "").strip()
        provider = str(contract.get("provider") or "external_ingress").strip() or "external_ingress"
        if interface:
            lines.append(f'        oifname "{interface}" accept comment "immunity: {provider} egress"')
    return lines

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
    trusted_client_ipv4_nft_set: str,
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
        f'        ip saddr {trusted_client_ipv4_nft_set} ip daddr @secure_dns_bypass_ipv4 meta l4proto tcp tcp dport {{ 443, 853 }} reject with tcp reset comment "reject secure DNS bypass TCP from LAN"',
        f'        ip saddr {trusted_client_ipv4_nft_set} ip daddr @secure_dns_bypass_ipv4 meta l4proto udp udp dport {{ 443, 853 }} reject with icmpx type port-unreachable comment "reject secure DNS bypass UDP from LAN"',
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
    disabled_output_guard_rules: list[str],
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
        *disabled_output_guard_rules,
        *_external_ingress_immunity_lines(),
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


def _is_disabled_subject(subject: dict[str, Any]) -> bool:
    path = str(subject.get("dataplane_path") or "").strip().lower()
    effective_mode = str(subject.get("effective_mode") or "").strip().lower()
    desired_mode = str(subject.get("desired_mode") or "").strip().lower()
    return path == "blocked" or effective_mode == "disabled" or desired_mode == "disabled"


def _as_nft_port(value: Any) -> int | None:
    try:
        port = int(value)
    except (TypeError, ValueError):
        return None
    return port if 0 < port <= 65535 else None


def _format_nft_ports(ports: set[int]) -> str:
    ordered = sorted(ports)
    if len(ordered) == 1:
        return str(ordered[0])
    return "{ " + ", ".join(str(port) for port in ordered) + " }"


def _reverse_ip_expr(expr: str) -> str | None:
    if expr == "ip saddr":
        return "ip daddr"
    if expr == "ip daddr":
        return "ip saddr"
    if expr == "ip6 saddr":
        return "ip6 daddr"
    if expr == "ip6 daddr":
        return "ip6 saddr"
    return None


def _build_disabled_input_guard_lines(subjects: list[dict[str, Any]]) -> list[str]:
    ports_by_proto: dict[str, set[int]] = {"tcp": set(), "udp": set()}
    subject_ids_by_key: dict[tuple[str, int], set[str]] = {}

    for subject in subjects:
        if not isinstance(subject, dict) or not subject.get("is_active"):
            continue
        if str(subject.get("subject_type") or "") == "fwrouter":
            continue
        if not _is_disabled_subject(subject):
            continue
        listeners = subject.get("network_listeners")
        if not isinstance(listeners, list):
            continue
        for listener in listeners:
            if not isinstance(listener, dict):
                continue
            proto = str(listener.get("proto") or "").strip().lower()
            if proto not in ports_by_proto:
                continue
            port = _as_nft_port(listener.get("port"))
            if port is None or port in CONTROL_PLANE_INPUT_PORTS:
                continue
            ports_by_proto[proto].add(port)
            subject_ids_by_key.setdefault((proto, port), set()).add(str(subject.get("subject_id") or "unknown"))

    lines: list[str] = []
    for proto in ("tcp", "udp"):
        ports = ports_by_proto[proto]
        if not ports:
            continue
        nft_ports = _format_nft_ports(ports)
        subject_ids = sorted(
            {
                subject_id
                for port in ports
                for subject_id in subject_ids_by_key.get((proto, port), set())
                if subject_id
            }
        )
        comment_subjects = ", ".join(subject_ids[:4])
        if len(subject_ids) > 4:
            comment_subjects = f"{comment_subjects}, +{len(subject_ids) - 4}"
        lines.extend(
            [
                f'        iifname "lo" meta l4proto {proto} {proto} dport {nft_ports} accept comment "allow local access to disabled service listener"',
                f'        iifname != "lo" meta l4proto {proto} {proto} dport {nft_ports} reject comment "disabled service listener: {comment_subjects}"',
            ]
        )
    return lines


def _build_disabled_forward_guard_lines(subjects: list[dict[str, Any]]) -> list[str]:
    lines: list[str] = []
    seen: set[tuple[str, str, str]] = set()
    for subject in subjects:
        if not isinstance(subject, dict) or not subject.get("is_active"):
            continue
        if str(subject.get("subject_type") or "") == "fwrouter" or not _is_disabled_subject(subject):
            continue
        scoped = subject.get("scoped_runtime")
        matcher = scoped.get("matcher") if isinstance(scoped, dict) else None
        if not isinstance(matcher, dict):
            continue
        expr = str(matcher.get("nft_expr") or "")
        val = str(matcher.get("value") or "")
        if not expr or not val:
            continue
        reverse_expr = _reverse_ip_expr(expr)
        candidates = [(expr, "tx"), (reverse_expr, "rx")] if reverse_expr else [(expr, "tx")]
        for candidate_expr, direction in candidates:
            if not candidate_expr:
                continue
            key = (candidate_expr, val, direction)
            if key in seen:
                continue
            seen.add(key)
            lines.append(
                f'        {candidate_expr} {val} reject with icmpx type admin-prohibited comment "disabled subject {direction} block: {subject.get("subject_id")}"'
            )
    return lines


def _build_disabled_output_guard_lines(subjects: list[dict[str, Any]]) -> list[str]:
    lines: list[str] = []
    seen_address_rules: set[tuple[str, str]] = set()
    uids_by_subject: dict[str, set[int]] = {}

    for subject in subjects:
        if not isinstance(subject, dict) or not subject.get("is_active"):
            continue
        if str(subject.get("subject_type") or "") == "fwrouter" or not _is_disabled_subject(subject):
            continue
        scoped = subject.get("scoped_runtime")
        matcher = scoped.get("matcher") if isinstance(scoped, dict) else None
        if isinstance(matcher, dict):
            expr = str(matcher.get("nft_expr") or "")
            val = str(matcher.get("value") or "")
            reverse_expr = _reverse_ip_expr(expr)
            if reverse_expr and val and (reverse_expr, val) not in seen_address_rules:
                seen_address_rules.add((reverse_expr, val))
                lines.append(
                    f'        {reverse_expr} {val} reject with icmpx type admin-prohibited comment "disabled subject host-to-target block: {subject.get("subject_id")}"'
                )

        raw_uids = subject.get("process_uids")
        if isinstance(raw_uids, list):
            for raw_uid in raw_uids:
                try:
                    uid = int(raw_uid)
                except (TypeError, ValueError):
                    continue
                if uid == ROOT_UID:
                    continue
                if 0 <= uid <= 4_294_967_295:
                    uids_by_subject.setdefault(str(subject.get("subject_id") or "unknown"), set()).add(uid)

    for subject_id, uids in sorted(uids_by_subject.items()):
        if not uids:
            continue
        nft_uids = _format_nft_ports(uids)
        lines.append(
            f'        meta skuid {nft_uids} reject with icmpx type admin-prohibited comment "disabled subject process egress block: {subject_id}"'
        )
    return lines
