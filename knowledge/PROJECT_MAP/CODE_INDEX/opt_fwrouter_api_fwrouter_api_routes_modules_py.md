# `/opt/fwrouter-api/fwrouter_api_routes_modules.py`

## Purpose

Exposes module state, integration lifecycle mode, desired-state changes, and supported module actions.

## Review Notes

Read the source file directly before changing related behavior. Check adjacent service, route, adapter, script, or systemd documentation as applicable.

## Runtime Impact

Adds `POST /api/v2/modules/{module_name}/lifecycle-mode` alongside the existing desired-state endpoint. The lifecycle endpoint accepts `none`, `managed`, or `external` where supported by the module.

## Guardrails

- Keep FWRouter core as the authority for classification and policy routing.
- Keep Mihomo as a VPN egress adapter, not the network policy engine.
- Preserve direct-safe behavior for host/control-plane traffic unless an explicit scoped contour says otherwise.
