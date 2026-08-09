# `/opt/fwrouter-api/fwrouter_api_services_xray.py`

## Purpose

Central service layer/facade for Xray client CRUD orchestration, subscriptions, runtime materialization, and public route entrypoints. Local client/subject state helpers live in `xray_client_state.py`. Status DTO assembly lives in `xray_status.py`. Runtime binding collection/state writing lives in `xray_bindings.py`. Low-level status helpers for bindings state, generated config egress summary, module rows, and materializable server checks live in `xray_runtime_state.py`.

## Review Notes

Read the source file directly before changing related behavior. Check adjacent service, route, adapter, script, or systemd documentation as applicable.

## Runtime Impact

`get_xray_status()` is imported from `xray_status.py` and re-exported through this module for route compatibility. Client CRUD, reload, and binding materialization remain managed-runtime responsibilities guarded at route boundaries. Binding and client-state helpers are imported from split modules and re-exported through this module for compatibility.

## Guardrails

- Keep FWRouter core as the authority for classification and policy routing.
- Keep Mihomo as a VPN egress adapter, not the network policy engine.
- Preserve direct-safe behavior for host/control-plane traffic unless an explicit scoped contour says otherwise.
