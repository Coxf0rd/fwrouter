# `/opt/fwrouter-api/fwrouter_api_services_ui_state.py`

## Purpose

Builds UI DTOs for router summary, client panels, settings inventory, display settings, and the admin-facing system visibility list. Log localization/summary logic lives in `ui_state_logs.py` and is re-exported here for compatibility.

## Review Notes

Read the source file directly before changing related behavior. Check adjacent service, route, adapter, script, or systemd documentation as applicable.

## Runtime Impact

Display settings are persisted in the SQLite `settings` table. The canonical visibility model is `system_visibility`; legacy `show_lan`, `show_tailscale`, `show_xray`, `show_docker`, and `show_host` fields are still synchronized for older UI code. Custom external systems can describe API management clients, external VPN egress modules, or external client sources with location/address/runtime/endpoints/capabilities. They are registration/display records and must not imply lifecycle control or routing-target creation. External management clients are auto-discovered from operational log `management_attribution` and shown as external API clients.

`_summarize_log_event` is kept as a facade import from `ui_state_logs.py`.

## Guardrails

- Keep FWRouter core as the authority for classification and policy routing.
- Keep Mihomo as a VPN egress adapter, not the network policy engine.
- Preserve direct-safe behavior for host/control-plane traffic unless an explicit scoped contour says otherwise.
- `/api/v2/ui/clients` must avoid cold live dataplane/Mihomo probes in its effective-subject read model. Use the cheap committed-state effective mode path so UI polling remains fast; runtime health belongs in dedicated runtime endpoints.
