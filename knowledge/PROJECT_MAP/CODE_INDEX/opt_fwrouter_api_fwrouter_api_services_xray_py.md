# `/opt/fwrouter-api/fwrouter_api_services_xray.py`

## Purpose

Central service layer for Xray status, clients, subscriptions, runtime bindings, and handoff assignments.

## Review Notes

Read the source file directly before changing related behavior. Check adjacent service, route, adapter, script, or systemd documentation as applicable.

## Runtime Impact

`get_xray_status()` includes the module lifecycle DTO so status callers can distinguish bundled managed Xray from an external/user-managed integration. Client CRUD, reload, and binding materialization remain managed-runtime responsibilities guarded at route boundaries.

## Guardrails

- Keep FWRouter core as the authority for classification and policy routing.
- Keep Mihomo as a VPN egress adapter, not the network policy engine.
- Preserve direct-safe behavior for host/control-plane traffic unless an explicit scoped contour says otherwise.
