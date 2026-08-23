# `/opt/fwrouter-api/fwrouter_api/services/ui_state.py`

## Purpose

Compatibility facade for UI read-model services. It keeps the old import surface
for routes, tests, runtime prewarm, and log routes while delegating real work to
focused modules.

## Review Notes

Delegated modules:

- `ui_state_settings.py` owns persisted display settings.
- `ui_state_common.py` owns shared helpers for activity, traffic labels, traffic maps, subscription grouping, and role normalization.
- `ui_state_clients.py` owns `/ui/clients` DTOs, filtering, panel counts, and client presence summaries.
- `ui_state_inventory.py` owns settings inventory DTOs.
- `ui_state_summary.py` owns router summary and settings workspace DTOs.

Keep facade signatures stable. Some regression tests monkeypatch old facade names
such as `_load_traffic_maps` and `list_subjects_with_effective_state`; the facade
syncs those hooks into the delegated modules before calling them.

## Runtime Impact

The facade itself should not add runtime behavior. Delegated modules read SQLite
state, write display settings, use short TTL read-model caches, and build DTOs
for UI polling endpoints.

`_summarize_log_event` is kept as a facade import from `ui_state_logs.py`.

## Guardrails

- Keep FWRouter core as the authority for classification and policy routing.
- Keep Mihomo as a VPN egress adapter, not the network policy engine.
- Preserve direct-safe behavior for host/control-plane traffic unless an explicit scoped contour says otherwise.
- `/api/v2/ui/clients` must avoid cold live dataplane/Mihomo probes in its effective-subject read model. Use the cheap committed-state effective mode path so UI polling remains fast; runtime health belongs in dedicated runtime endpoints.
- User-facing activity reason and traffic metric labels must come from `UI_TEXT_REGISTRY` in `ui_state_logs.py`; do not add local display strings in `ui_state.py` for new backend machine keys.
- Preserve old facade monkeypatch hooks when moving internals again.
