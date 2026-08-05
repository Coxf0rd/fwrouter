# `/opt/fwrouter-api/fwrouter_api_services_mihomo.py`

## Purpose

Service-level facade for Mihomo status and inventory sync.

## Review Notes

Read the source file directly before changing related behavior. Check adjacent service, route, adapter, script, or systemd documentation as applicable.

## Runtime Impact

`get_mihomo_status()` reads the live controller and includes the `vpn` module DTO so API callers can see `lifecycle_mode` ownership alongside runtime health. Persistent runtime writes are owned by the config/runtime services and their route guards.

## Guardrails

- Keep FWRouter core as the authority for classification and policy routing.
- Keep Mihomo as a VPN egress adapter, not the network policy engine.
- Preserve direct-safe behavior for host/control-plane traffic unless an explicit scoped contour says otherwise.
