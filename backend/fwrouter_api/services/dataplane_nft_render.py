from __future__ import annotations

from typing import Any, Callable

from fwrouter_api.services.dataplane_global import build_nft_rule_sets, read_effective_rules_artifact
from fwrouter_api.services.dataplane_nft_chains import (
    _build_classify_chain_lines,
    _build_disabled_forward_guard_lines,
    _build_disabled_input_guard_lines,
    _build_disabled_output_guard_lines,
    _build_output_entry_chain_lines,
    _build_output_nat_chain_lines,
    _build_prerouting_entry_chain_lines,
    _build_prerouting_nat_chain_lines,
    _build_terminal_direct_chain_lines,
    _build_terminal_vpn_chain_lines,
    _build_vpn_mark_chain_lines,
    _nft_port_match_value,
)
from fwrouter_api.services.dataplane_nft_constants import (
    OWNED_TABLE,
    STATIC_SECURE_DNS_BYPASS_IPV4,
    _derive_mark_hex,
)
from fwrouter_api.services.dataplane_nft_sets import (
    _build_scoped_vpn_sets,
    _read_manifest_extra_ipv4_list,
    _render_dns_runtime_set,
    _render_set,
    _resolve_lan_ingress_interfaces,
    _resolve_rules_effective_artifact,
    _safe_set_suffix,
)
from fwrouter_api.services.subject_taxonomy import subject_needs_transparent_policy


def render_owned_table_candidate(
    manifest: dict[str, Any] | None = None,
    *,
    rules_effective_loader: Callable[[], dict[str, Any] | None] = read_effective_rules_artifact,
) -> str:
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

    rules_effective = _resolve_rules_effective_artifact(
        manifest,
        rules_effective_loader=rules_effective_loader,
    )
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
                if not subject_needs_transparent_policy(str(subject.get("subject_type") or "")):
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
    transparent_tcp_rx_ports = _nft_port_match_value([vpn_redir_port, full_vpn_redir_port])
    transparent_udp_rx_ports = _nft_port_match_value([vpn_tproxy_port, full_vpn_tproxy_port])

    traffic_counter_subjects = [
        subject
        for subject in active_subjects
        if subject_needs_transparent_policy(str(subject.get("subject_type") or ""))
    ]

    for subject in traffic_counter_subjects:
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
                if transparent_tcp_rx_ports:
                    vpn_rx_counter_rules.append(
                        f'        meta mark != {proxy_bypass_mark_hex} meta l4proto tcp tcp sport {transparent_tcp_rx_ports} {rx_expr} {val} counter name "cnt_{slug}_vpn_rx"'
                    )
                if transparent_udp_rx_ports:
                    vpn_rx_counter_rules.append(
                        f'        meta mark != {proxy_bypass_mark_hex} meta l4proto udp udp sport {transparent_udp_rx_ports} {rx_expr} {val} counter name "cnt_{slug}_vpn_rx"'
                    )

    scoped_steering_rules: list[str] = []
    system_output_steering_rules: list[str] = []
    disabled_forward_guard_rules = _build_disabled_forward_guard_lines(active_subjects)
    disabled_output_guard_rules = _build_disabled_output_guard_lines(active_subjects)
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

        if path == "blocked":
            if family == "ipv6":
                scoped_steering_rules.append(
                    f'        {expr} {val} reject with icmpv6 type admin-prohibited comment "scoped disabled block: {subject.get("subject_id")}"'
                )
            elif family == "ipv4":
                scoped_steering_rules.append(
                    f'        {expr} {val} reject with icmpx type admin-prohibited comment "scoped disabled block: {subject.get("subject_id")}"'
                )
            else:
                scoped_steering_rules.append(
                    f'        {expr} {val} drop comment "scoped disabled block: {subject.get("subject_id")}"'
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
        *_build_disabled_input_guard_lines(active_subjects),
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
                disabled_output_guard_rules=disabled_output_guard_rules,
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
            *disabled_forward_guard_rules,
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
