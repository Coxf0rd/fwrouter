# `/opt/fwrouter-api/fwrouter_api_routes_ui.py`

## Purpose

UI read-model routes for router summary, external IP indicator, clients, settings workspace, settings inventory, and display settings.

## Review Notes

Read the source file directly before changing related behavior. Check adjacent service, route, adapter, script, or systemd documentation as applicable.

## Runtime Impact

`PUT /api/v2/ui/settings/display` persists operator UI preferences in SQLite through `services/ui_state.py`. The request uses role-based `system_visibility`, hidden subjects, and traffic metric preferences; external connection instances are not stored in display settings. `GET /api/v2/ui/settings/inventory` filters by legacy role via `role=lan_client|external_network_source|vless_client|docker_runtime|host_runtime|router_core`, returns compatible `kind`/`inventory_role`, exposes derived `domain_category` for user-facing grouping, keeps concrete runtime detail in `implementation_kind` / `implementation_label`, and accepts `include_inactive=true` for Settings management views; admin devices use the default filtered view. `GET /api/v2/ui/external-connections/{connection_id}/contract` exposes the normalized JSON contract for a registered external connection without mutating state. `POST /api/v2/ui/external-connections/preview` validates and normalizes a custom external connection draft without saving it. `POST /api/v2/ui/external-connections` creates a connection with a backend-generated immutable `connection_id`. `PUT/PATCH/DELETE /api/v2/ui/external-connections/{connection_id}` manage only custom external connection records, preserving the rest of display settings and rejecting immutable `connection_id`/`connection_type`/`replacement_target` changes or invalid collector JSON with field-level errors.

## Guardrails

- Keep FWRouter core as the authority for classification and policy routing.
- Keep Mihomo as a VPN egress adapter, not the network policy engine.
- Preserve direct-safe behavior for host/control-plane traffic unless an explicit scoped contour says otherwise.
