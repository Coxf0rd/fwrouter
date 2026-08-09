# `/opt/fwrouter-api/fwrouter_api/services/ui_display_settings.py`

## Purpose

Owns UI display preferences and the Settings "Connections" read model after
the split from `ui_state.py`.

## Runtime Impact

Reads/writes the `ui.admin_client_display.v1` row in SQLite `settings`.
Builds the list of builtin systems, custom external connections, and
auto-discovered external management clients from operational log attribution.

## Guardrails

- `custom_external_systems` are registration/display records only; do not make
  them lifecycle-controlled runtimes from this module.
- Keep `system_visibility` canonical while preserving legacy `show_*` fields
  through `ui_state.py` compatibility.
- External VPN module records can expose guide/readiness metadata, but actual
  dataplane support belongs in the external VPN adapter path.
