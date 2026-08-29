# `/etc/systemd/system/fwrouter_api_service.md`

## Purpose

Starts the FWRouter FastAPI backend as the routing core control plane.

## Review Notes

Read the source file directly before changing related behavior. Check adjacent service, route, adapter, script, or systemd documentation as applicable.

## Runtime Impact

`fwrouter-api.service` orders only after `network-online.target`. Its `ExecStartPre` runs core host preflight and must not make Docker, Mihomo, Xray, dnsmasq, or TUN mandatory for backend startup.

The service is hardened but still runs as root with a narrow capability set:
`NoNewPrivileges=yes`, `ProtectSystem=full`, `ProtectHome=yes`, `PrivateTmp=yes`,
`CapabilityBoundingSet=CAP_NET_ADMIN CAP_NET_RAW`, and
`RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6 AF_NETLINK`.
Explicit writable paths are `/var/lib/fwrouter-v2`, `/var/log/fwrouter`,
`/run/fwrouter-v2`, `/etc/dnsmasq.d`, and `/etc/iproute2/rt_tables.d`.

## Guardrails

- Keep FWRouter core as the authority for classification and policy routing.
- Keep Mihomo as a VPN egress adapter, not the network policy engine.
- Preserve direct-safe behavior for host/control-plane traffic unless an explicit scoped contour says otherwise.
- Retest API health, runtime inventory, selector state, routing apply, dataplane helpers, and restart recovery before narrowing systemd privileges further.
