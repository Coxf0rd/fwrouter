# `/opt/fwrouter-api/fwrouter_api/services/ui_display_settings_display.py`

## Purpose

Builds Settings > Connections display-system rows from builtin templates, modules, inventory, custom external connections, and auto-discovered external clients.

## Important Functions

- `_display_systems(...)`
- `_external_network_source_display_systems(...)`
- `_external_management_display_systems(...)`
- `_builtin_external_connection_by_id(...)`

## Notes

- Discovered external network sources must appear as provider-specific rows such as `external-network-tailscale`.
- The generic `external_network_source` role must not replace a concrete provider row.
