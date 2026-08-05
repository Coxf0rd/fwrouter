# `/opt/fwrouter-api/fwrouter_api_routes_xray.py`

## Purpose

API routes for Xray status, managed client CRUD, runtime reload, subject sync, and subscription export.

## Review Notes

Read the source file directly before changing related behavior. Check adjacent service, route, adapter, script, or systemd documentation as applicable.

## Runtime Impact

Read-only status/list/export endpoints can inspect Xray state. Routes stay thin: service-layer client CRUD, subject sync, reload, binding materialization, and profile reconcile actions enforce `xray.lifecycle_mode=managed` before adapter/config/reload calls.

## Guardrails

- Keep FWRouter core as the authority for classification and policy routing.
- Keep Mihomo as a VPN egress adapter, not the network policy engine.
- Preserve direct-safe behavior for host/control-plane traffic unless an explicit scoped contour says otherwise.
