# `/opt/fwrouter-api/fwrouter_api_services_dataplane_nft.py`

## Purpose

Renders FWRouter-owned `inet fwrouter_v2` nftables candidates from the dataplane
manifest. It owns protected/direct/VPN sets, transparent Mihomo contours,
traffic counters and subject-specific steering.

DNS runtime sets (`dns_direct_ipv4` / `dns_vpn_ipv4`) are plain timeout sets.
They intentionally do not use `interval` / `auto-merge`: dnsmasq only
materializes single IPv4 answers, and interval sets add unnecessary kernel
work and have been observed to destabilize dnsmasq under high DNS churn.

## Review Notes

Read the source file directly before changing related behavior. Check adjacent service, route, adapter, script, or systemd documentation as applicable.

## Runtime Impact

Disabled subjects are enforced here. IP-addressable subjects are rejected from
`fwrouter_classify`, from `forward` in both directions, and from `output` for
host-origin traffic to the subject IP. Host-network Docker and host service
listeners are rejected from the `input` chain for non-loopback traffic. Generic
listener blocking skips SSH, DNS/DHCP and FWRouter control-plane ports.
Non-root host-network Docker process egress is rejected with `meta skuid`; UID
`0` is deliberately skipped because it is shared with the host control plane.

## Guardrails

- Keep FWRouter core as the authority for classification and policy routing.
- Keep Mihomo as a VPN egress adapter, not the network policy engine.
- Preserve direct-safe behavior for host/control-plane traffic unless an explicit scoped contour says otherwise.
