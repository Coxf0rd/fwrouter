# `/etc/systemd/system/fwrouter_api_service.md`

## Purpose

Starts the FWRouter FastAPI backend as the routing core control plane.

## Review Notes

Read the source file directly before changing related behavior. Check adjacent service, route, adapter, script, or systemd documentation as applicable.

## Runtime Impact

`fwrouter-api.service` orders only after `network-online.target`. Its `ExecStartPre` runs core host preflight and must not make Docker, Mihomo, Xray, dnsmasq, or TUN mandatory for backend startup.

## Guardrails

- Keep FWRouter core as the authority for classification and policy routing.
- Keep Mihomo as a VPN egress adapter, not the network policy engine.
- Preserve direct-safe behavior for host/control-plane traffic unless an explicit scoped contour says otherwise.
