# Exact function from installed-code snapshot c1e00b56 (same backend as 3d285c3).
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
