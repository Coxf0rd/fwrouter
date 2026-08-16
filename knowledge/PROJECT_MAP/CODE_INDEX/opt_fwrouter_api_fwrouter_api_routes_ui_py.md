# `/opt/fwrouter-api/fwrouter_api_routes_ui.py`

## Purpose

UI read-model routes for router summary, external IP indicator, clients, settings workspace, settings inventory, and display settings.

## Review Notes

Read the source file directly before changing related behavior. Check adjacent service, route, adapter, script, or systemd documentation as applicable.

## Runtime Impact

`PUT /api/v2/ui/settings/display` persists operator UI preferences in SQLite through `services/ui_state.py`. The request uses the role-based `system_visibility` map and display-only `custom_external_systems`. `GET /api/v2/ui/settings/inventory` filters by role via `role=lan_client|external_network_source|vless_client|docker_runtime|host_runtime`, returns role-based `kind`/`inventory_role`, keeps concrete runtime detail in `implementation_kind`, and accepts `include_inactive=true` for Settings management views; admin devices use the default filtered view. `GET /api/v2/ui/external-connections/{system_id}/contract` exposes the normalized JSON contract for a registered external connection or auto-discovered external management client without mutating state. `POST /api/v2/ui/external-connections/preview` validates and normalizes a custom external connection draft without saving it. `PUT/PATCH/DELETE /api/v2/ui/external-connections/{system_id}` manage only custom external connection records, preserving the rest of display settings and rejecting immutable `system_id`/`connection_type`/`replacement_target` changes or invalid collector JSON with field-level errors.

## Guardrails

- Keep FWRouter core as the authority for classification and policy routing.
- Keep Mihomo as a VPN egress adapter, not the network policy engine.
- Preserve direct-safe behavior for host/control-plane traffic unless an explicit scoped contour says otherwise.
