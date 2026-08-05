# `/opt/fwrouter-api/fwrouter_api_routes_mihomo.py`

## Purpose

API routes for Mihomo status, inventory sync, config status/promote/reconcile, and container restart.

## Review Notes

Read the source file directly before changing related behavior. Check adjacent service, route, adapter, script, or systemd documentation as applicable.

## Runtime Impact

Read-only status/config endpoints can inspect Mihomo state. Routes stay thin: service-layer promote/reconcile/restart actions enforce `vpn.lifecycle_mode=managed` before validation, config writes, Docker, or container restart calls.

## Guardrails

- Keep FWRouter core as the authority for classification and policy routing.
- Keep Mihomo as a VPN egress adapter, not the network policy engine.
- Preserve direct-safe behavior for host/control-plane traffic unless an explicit scoped contour says otherwise.
