# `/opt/fwrouter-api/fwrouter_api_routes_ui.py`

## Purpose

UI read-model routes for router summary, external IP indicator, clients, settings workspace, settings inventory, and display settings.

## Review Notes

Read the source file directly before changing related behavior. Check adjacent service, route, adapter, script, or systemd documentation as applicable.

## Runtime Impact

`PUT /api/v2/ui/settings/display` persists operator UI preferences in SQLite through `services/ui_state.py`. The request accepts legacy `show_*` fields plus the generic `system_visibility` map and display-only `custom_external_systems`.

## Guardrails

- Keep FWRouter core as the authority for classification and policy routing.
- Keep Mihomo as a VPN egress adapter, not the network policy engine.
- Preserve direct-safe behavior for host/control-plane traffic unless an explicit scoped contour says otherwise.
