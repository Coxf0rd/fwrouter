# `/opt/fwrouter-api/fwrouter_api_services_xray.py`

## Purpose

Central service layer for Xray clients, subscriptions, runtime bindings, and handoff assignments. Low-level status helpers for bindings state, generated config egress summary, module rows, and materializable server checks live in `xray_runtime_state.py`.

## Review Notes

Read the source file directly before changing related behavior. Check adjacent service, route, adapter, script, or systemd documentation as applicable.

## Runtime Impact

`get_xray_status()` includes the module lifecycle DTO so status callers can distinguish bundled managed Xray from an external/user-managed integration; it imports the low-level state helpers from `xray_runtime_state.py`. Client CRUD, reload, and binding materialization remain managed-runtime responsibilities guarded at route boundaries.

## Guardrails

- Keep FWRouter core as the authority for classification and policy routing.
- Keep Mihomo as a VPN egress adapter, not the network policy engine.
- Preserve direct-safe behavior for host/control-plane traffic unless an explicit scoped contour says otherwise.
