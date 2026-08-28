# Network Model

## Authority Split

FWRouter core is the single authority for traffic classification, subject policy, and host policy routing. Mihomo is a mostly dumb VPN egress adapter. Xray is a separate ingress/subscription runtime whose client traffic is forced to VPN egress.

## Subject Roles

Client-plane subjects:

- `lan`
- `external_network_client`
- `explicit_external_client`

System/control subjects:

- `host`
- `docker`
- `fwrouter`

All subject types remain in the shared inventory, but they do not participate in routing the same way.

## Default Paths

- LAN clients follow global direct/selective/vpn policy plus per-subject overrides.
- External network clients follow the same client-plane model when attributed to a concrete `connection_id`.
- Explicit external clients use forced VPN through their provider handoff/binding path.
- Host and Docker traffic default to direct-safe and require stable attribution for explicit scoped VPN.
- FWRouter own traffic is always direct-safe by default. `fwrouter:global` must not make all host output VPN-bound.

## LAN DNS Contract

LAN clients must use router DNS so domain-aware selective routing can materialize destination IPs into nftables timeout sets. DHCP must not advertise public secondary DNS. DNS capture must run before VPN classification.

Protected/control-plane destinations are direct-safe and include local ranges, LAN/service networks, management SSH, Tailscale transport/control needs, custom upstream proxy IPs, and backend dependencies required for apply/watchdog/selector recovery.

The canonical protected/trusted network lists and LAN interface filters are configured in `/opt/fwrouter-api/.env` and normalized by `services/network_contract.py`. The same contract feeds effective protected rules, nft protected sets, trusted transparent-ingress guards, dnsmasq LAN binding discovery, and apply-time conntrack cleanup. Defaults preserve the current deployment baseline, but the env block accepts any valid deployment CIDR/interface names; moving LAN layout should not require Python/shell code edits.

## Transparent Dataplane

FWRouter uses nftables and policy routing to steer marked traffic:

- selective TCP -> Mihomo redir listener `5202`
- selective UDP -> Mihomo TProxy listener `5203`
- full-VPN TCP -> Mihomo full redir listener `5204`
- full-VPN UDP -> Mihomo full TProxy listener `5205`

Mihomo is the default managed VPN adapter. A configured `external_vpn_module`
can replace only this transparent handoff when it provides local
`tcp_redir_port` and `udp_tproxy_port` endpoints; optional
`full_tcp_redir_port` and `full_udp_tproxy_port` override full-VPN handoff.
HTTP/SOCKS endpoints remain documentation/explicit-proxy metadata and are not
used by nftables transparent routing.

Policy routing table `100 fwrouter_vpn` exists only when a candidate requires transparent VPN routing.

## Control-Plane Safety

Local, protected, management, and control-plane dependency traffic must stay direct. If a dependency is missing from the protected contour, treat it as a state-model gap rather than normal selective behavior.

## Routing Modes

- `direct`: default path is direct; no broad transparent VPN path unless scoped VPN/selective subjects require it.
- `selective`: only configured VPN domains/IPs and materialized DNS matches go to VPN; unmatched traffic stays direct.
- `vpn`: eligible client-plane traffic goes to VPN except protected/direct-safe destinations.

## Risks

- Public DNS bypass makes domain-aware selective routing incomplete.
- Broad fallback-to-VPN in selective mode breaks the intended model.
- Host output tied to `fwrouter:global` can cut off SSH/control-plane recovery.
- Live kernel state disappears after reboot and must be restored from persistent intent.
