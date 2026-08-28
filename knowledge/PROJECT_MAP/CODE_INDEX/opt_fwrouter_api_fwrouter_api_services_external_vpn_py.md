# `/opt/fwrouter-api/fwrouter_api/services/external_vpn.py`

## Purpose

Resolves a ready custom `external_vpn_module` from UI display settings and
exposes it as a transparent VPN adapter for dataplane preflight/apply. Callers
that need role-specific behavior use
`active_external_vpn_module_for_replacement_target(...)`.

## Runtime Impact

This service only reads persistent UI settings through a short live-probe cache.
It does not create services, containers, or lifecycle actions. A module is
considered usable for dataplane only when it has local `tcp_redir_port` and
`udp_tproxy_port` and the runtime looks ready: `healthcheck_url` returns a
non-error/ready status when set, otherwise a quick TCP connect to
`tcp_redir_port` succeeds. Optional `full_tcp_redir_port` and
`full_udp_tproxy_port` override full-VPN handoff. The compatibility helper
`active_external_vpn_module()` resolves the `mihomo` replacement target; generic
registry code should use the target-aware helper. Legacy records with an empty
replacement target are treated as `mihomo` replacement for compatibility.

## Guardrails

- Use only for `external_vpn_module`, not management clients or external network sources.
- Do not treat HTTP/SOCKS endpoints as transparent nft routing targets.
- Missing settings table or missing/incomplete records must degrade to “no external VPN module”.
