# API And CLI

## Main API Entrypoint

- service: `fwrouter-api.service`
- module: `/opt/fwrouter-api/fwrouter_api/main.py`
- listen address: `127.0.0.1:5000`
- API prefix: `/api/v2`

## Key API Groups

- `system`, `runtime`, `state`, `modules`, `core/bypass`
- `subjects`, `system-subjects`
- `servers`, `routing/global`, subject server overrides
- `rules`
- `mihomo`
- `xray`
- `subscription`, `selector`, `server-ping`
- `traffic`
- `jobs`
- `transfer/control-plane`
- `watchdog`
- `logs`
- `ui`
- `operations`: `apply/dry-run`, `maintenance/cleanup`, `full-refresh`

## CLI / Runner Entrypoints

- `fwrouter-api = fwrouter_api.main:run`
- `fwrouter = fwrouter_api.cli:main`
- `python -m fwrouter_api_maintenance`
- `/usr/local/libexec/fwrouter/fwrouter-xray-sub-gateway.py`
- shell scripts in `/opt/fwrouter-api/scripts/`
- shell scripts in `/usr/local/libexec/fwrouter/`

## Important Operational Endpoints

- `GET /api/v2/health`
- `GET /api/v2/runtime`
- `GET /api/v2/runtime/scoped-egress`
- `GET /api/v2/state/system`
- `GET /api/v2/state/modules`
- `GET /api/v2/state/subjects`
- `GET /api/v2/state/subjects/{subject_id}`
- `GET /api/v2/state/routing`
- `GET /api/v2/state/watchdog`
- `GET /api/v2/state/rules`
- `GET /api/v2/state/xray`
- `GET /api/v2/state/vpn`
- `GET /api/v2/reconcile`
- `GET /api/v2/events/recent`
- `GET /api/v2/core/bypass`
- `POST /api/v2/core/bypass/enable`
- `POST /api/v2/core/bypass/disable`
- `GET /api/v2/modules`
- `POST /api/v2/modules/{module_name}/lifecycle-mode`
- `GET/POST /api/v2/routing/global`
- `GET /api/v2/servers`
- `POST /api/v2/mihomo/config/reconcile`
- `POST /api/v2/xray/reload`
- `POST /api/v2/traffic/collect`
- `POST /api/v2/maintenance/cleanup`
- `GET /api/v2/ui/whoami`
- `GET /api/v2/ui/settings/inventory`
- `POST /api/v2/ui/external-connections`
- `GET /api/v2/ui/external-connections/{connection_id}/contract`

## External Management Clients

The external management contract is documented in `EXTERNAL_MANAGEMENT.md`.

Short form: use `requested_by="external_client:<client_name>"` and include `management_context` with at least `client_name` and `action`.

If external attribution is incomplete, the backend returns `MANAGEMENT_ATTRIBUTION_INCOMPLETE` before executing the requested action.

## Notes

- `/api/v2/ui/clients` is a full, heavy read model for the admin client panel. The user view must not call it just to identify the current client.
- `/api/v2/state/*` endpoints expose a read-only normalized state projection. They separate intent, execution, observation, reconcile, identity, effective state, reason, and user/admin projection without changing legacy state fields or UI read models.
- `/api/v2/reconcile` exposes a shared read-only reconcile snapshot for modules, subjects, Xray bindings, routing, VPN adapter health, and watchdog. It compares intent, execution/apply state, runtime observation, and projection state without repair and without changing database or runtime state.
- `/api/v2/events/recent` exposes the new read-only events view with `audit`, `operational`, `diagnostic`, and aggregation summary. It adapts legacy `operational_logs` without migration and hides diagnostic/noise events from the new operational list.
- `fwrouter reconcile check` uses the same read-only reconcile service and prints a short operational summary (`SYSTEM OK` or drift/stale/failed counts).
- `/api/v2/ui/whoami` returns the current LAN/external ingress subject by IP with `effective_state`, making it the lightweight source for `mode_source` and `effective_mode` in user UI.
- `DELETE /api/v2/subjects/{subject_id}/mode` clears a user mode override and returns the client to global mode inheritance; it does not change manual VPN server selection.
- Mutating endpoints may accept `requested_by` as opaque attribution for UI, CLI, scheduler, or external management clients. `external_client` requests must include enough `management_context` (`client_name`, `action`).
- `POST /api/v2/core/bypass/enable|disable` requires `confirm_apply=true`; bypass changes runtime/dataplane core state through a job, not through a direct synchronous toggle.
- `POST /api/v2/maintenance/cleanup` creates a `maintenance_cleanup` job; `dry_run=true` is the default.
- Module DTOs expose `lifecycle_mode` (`none`, `managed`, `external`), `installed`, and `manageable_actions`. External integrations are probe-only; module lifecycle actions are not exposed through the generic modules API.
- `GET /api/v2/servers` returns real server inventory by default. The Xray-only virtual target `virtual:xray:vpn-auto` is included only when `include_virtual_xray_vpn_auto=true`; it must not be saved into the normal Mihomo `vpn-auto` membership.
