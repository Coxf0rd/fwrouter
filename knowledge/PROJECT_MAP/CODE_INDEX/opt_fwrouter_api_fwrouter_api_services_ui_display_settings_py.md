# `/opt/fwrouter-api/fwrouter_api/services/ui_display_settings.py`

## Purpose

Owns UI display preferences and the Settings "Connections" read model after
the split from `ui_state.py`.

## Runtime Impact

Reads/writes the `ui.admin_client_display.v1` row in SQLite `settings`.
Builds the list of builtin systems from role-based real data, concrete external
network sources discovered from subject inventory, custom external connections,
and auto-discovered external management clients from operational log
attribution. Builtin display IDs are generic UI roles: `lan`,
`external_network_source`, `vless_client`, `vpn_runtime`, `docker`, and `host`;
concrete discovered sources such as `external-network-tailscale` are separate
connection rows. Generic role summaries may set `show_in_connections=false` so
the Settings "Connections" list shows the real implementation instead of only a
role label.
Connection guides expose stable `external_system_id`, `requested_by`, and
`collector` values so an external client can mount itself to the UI-created
record. `external_connection_contract(...)` exposes the same normalized guide,
identity, and readiness DTO for registered records and auto-discovered external
management clients via `GET /api/v2/ui/external-connections/{system_id}/contract`.
Traffic accounting resolves `metadata.external_system_id` through this same
settings row.
External module readiness uses `runtime_adapters`, so active status is reported
by role (`vpn_dataplane` or `explicit_client_runtime`) rather than by a
display-specific implementation key.
Custom records also carry optional `replacement_target` metadata (`mihomo`,
`xray`, or empty). `mihomo` is a working external VPN dataplane replacement
when the external module is ready; `xray` is a generic explicit-client runtime
contract with identity/traffic/readiness API until a dedicated compatible
adapter is implemented.
Custom records also carry `integration_mode`, `refresh_mode`, and
`collector_config`. The public guide includes a `collection` block so developers
can choose push-on-change, manual refresh, or interval polling without guessing
which backend endpoint to use.
`preview_custom_external_connection(...)` validates a draft without saving and
returns the backend-normalized record/contract. `upsert_custom_external_connection(...)`
and `delete_custom_external_connection(...)` mutate only one custom external
connection in the settings row; updates reject immutable `system_id`,
`connection_type`, and `replacement_target` changes. Collector config, endpoint,
and capability keys are allowlisted and reported as field-level validation
errors instead of being silently dropped.
Discovered external network sources such as `external-network-tailscale` are
`customizable=true`; a `PATCH` promotes them to a custom override with the same
`system_id` instead of mutating runtime inventory directly. Deleting the custom
override reveals the discovered row again.
Provider-specific labels, system ids, refresh mode, and collector defaults for
discovered external network sources come from `subject_taxonomy`, not local
provider branches in this UI read model.

## Guardrails

- `custom_external_systems` are registration/display records only; do not make
  them lifecycle-controlled runtimes from this module.
- Keep `system_visibility` as the only display-visibility contract.
- Do not replace concrete external implementations with generic role labels in
  the Settings "Connections" list. If real inventory shows Tailscale, expose a
  `Tailscale` connection row derived from that runtime data.
- External VPN module records can expose guide/readiness metadata, but actual
  dataplane support belongs in the external VPN adapter path.
- Guides for `external_vpn_module` and `external_network_source` include
  `/traffic/collect` examples. `external_management` remains API-control only.
- `api_push` external connections are not polled. `manual` collectors run only
  via `/ui/external-connections/{system_id}/collect`; `interval` collectors are
  handled by `external_collectors.py`.
- `api_push` always normalizes to `refresh_mode=on_change`; pull integrations
  use only `manual` or `interval`. Required collector fields are `url` for
  `http_poll`, `script_id` for `command_probe`, and `path` for `file_read`.
- Readiness exposes missing endpoints and `active_as_runtime_adapter` for
  external runtime modules.
