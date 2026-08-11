# `/opt/fwrouter-api/fwrouter_api/services/ui_display_settings.py`

## Purpose

Owns UI display preferences and the Settings "Connections" read model after
the split from `ui_state.py`.

## Runtime Impact

Reads/writes the `ui.admin_client_display.v1` row in SQLite `settings`.
Builds the list of builtin systems, custom external connections, and
auto-discovered external management clients from operational log attribution.
Connection guides expose stable `external_system_id`, `requested_by`, and
`collector` values so an external client can mount itself to the UI-created
record. Traffic accounting resolves `metadata.external_system_id` through this
same settings row.

## Guardrails

- `custom_external_systems` are registration/display records only; do not make
  them lifecycle-controlled runtimes from this module.
- Keep `system_visibility` canonical while preserving legacy `show_*` fields
  through `ui_state.py` compatibility.
- External VPN module records can expose guide/readiness metadata, but actual
  dataplane support belongs in the external VPN adapter path.
- Guides for `external_vpn_module` and `external_network_source` include
  `/traffic/collect` examples. `external_management` remains API-control only.
