# `/opt/fwrouter-api/fwrouter_api_services_modules.py`

## Purpose

Owns module desired/runtime/apply state and integration lifecycle mode DTOs.

## Review Notes

Read the source file directly before changing related behavior. Check adjacent service, route, adapter, script, or systemd documentation as applicable.

## Runtime Impact

Reads and updates the `modules` table. `fetch_modules()` enriches existing rows with `lifecycle_mode`, `installed`, and `manageable_actions` but does not create optional provider/runtime rows. `set_module_lifecycle_mode()`, `set_module_desired_state()`, and explicit managed runtime mutation guards may create an optional row on demand for user/API action. `require_managed_module()` and `managed_runtime_operation_blocked()` are shared service-layer guards for runtime operations that write configs or reload containers. External integrations are user-managed and reject those operations before adapter/Docker/systemd calls run. External ingress modules support only `external`/`none`; legacy managed lifecycle actions are absent.

## Guardrails

- Keep FWRouter core as the authority for classification and policy routing.
- Keep Mihomo as a VPN egress adapter, not the network policy engine.
- Preserve direct-safe behavior for host/control-plane traffic unless an explicit scoped contour says otherwise.
