# `/opt/fwrouter-api/fwrouter_api_services_ui_state.py`

## Purpose

Builds UI DTOs for router summary, client panels, and settings inventory. Display settings and the admin-facing system visibility/Connections list live in `ui_display_settings.py`; log localization/summary logic lives in `ui_state_logs.py`. Both are imported here for compatibility with existing routes/tests.

## Review Notes

Read the source file directly before changing related behavior. Check adjacent service, route, adapter, script, or systemd documentation as applicable.

## Runtime Impact

Display settings are persisted in the SQLite `settings` table through `ui_display_settings.py`. The visibility contract is the role-based `system_visibility` map. Settings inventory exposes role-based `kind`/`inventory_role` (`lan_client`, `external_network_source`, `vless_client`, `docker_runtime`, `host_runtime`) and keeps the concrete adapter in `implementation_kind`, so UI tabs do not depend on implementation names. External network inventory rows also expose `display_system_id`, which lets the admin UI apply concrete source visibility without guessing implementation-specific names. Custom external systems remain registration/display records and must not imply lifecycle control or routing-target creation.

`_summarize_log_event` is kept as a facade import from `ui_state_logs.py`.

## Guardrails

- Keep FWRouter core as the authority for classification and policy routing.
- Keep Mihomo as a VPN egress adapter, not the network policy engine.
- Preserve direct-safe behavior for host/control-plane traffic unless an explicit scoped contour says otherwise.
- `/api/v2/ui/clients` must avoid cold live dataplane/Mihomo probes in its effective-subject read model. Use the cheap committed-state effective mode path so UI polling remains fast; runtime health belongs in dedicated runtime endpoints.
