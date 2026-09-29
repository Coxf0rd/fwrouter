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
- `subscription`, `selector`, `server-ping`, and explicit logical-server manual checks
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
- `GET /api/v2/diagnose`
- `GET /api/v2/events/recent`
- `GET /api/v2/core/bypass`
- `POST /api/v2/core/bypass/enable`
- `POST /api/v2/core/bypass/disable`
- `GET /api/v2/modules`
- `POST /api/v2/modules/{module_name}/lifecycle-mode`
- `GET/POST /api/v2/routing/global`
- `GET /api/v2/servers`
- `POST /api/v2/servers/manual-check` runs a provider-neutral canonical health refresh for `admin_all`, `user_global`, or `user_vpn_auto`; it reports group/member aggregates from freshly re-read canonical topology. `POST /api/v2/servers/{server_id}/manual-check` remains the per-group compatibility operation.
- `POST /api/v2/mihomo/config/reconcile`
- `POST /api/v2/subscription/refresh`
  - accepts quickly with `accepted`, `job_id`/`job`, `operation=subscription_refresh`, and lifecycle `stages`
  - actual download/parse/persist/Mihomo apply work runs in the existing jobs framework
  - poll `GET /api/v2/jobs/{job_id}`; success is reported only after Mihomo runtime reconcile/verification succeeds
- `POST /api/v2/xray/reload`
- `GET /api/v2/xray` includes a read-only `data.xray.vpn_auto_reconcile` pending-state DTO (`revision`, `pending`, `status`, `due_at`, `trigger`, timestamps, retry/error metadata, `applied_revision`, and `attempt_count`).
- `POST /api/v2/xray/vpn-auto/reconcile` records an immediate expected revision and queues the existing JobManager reconcile; poll the returned job. It does not report success until the claimed revision completes final runtime/public-profile verification.
- `DELETE /api/v2/xray/subscription-profiles/{token_or_reference}`; the compatibility form accepts an existing token/slug, while Settings inventory uses the exact non-secret `subscription-account:<account_id>` reference.
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
- `/api/v2/state/*` endpoints expose a read-only normalized state projection. They separate intent, execution, observation, reconcile, identity, effective state, reason, and user/admin projection. User-facing `projection.state` uses the unified health contract: `healthy`, `warning`, `degraded`, `failed`, `inactive`, `disabled`, `unknown`.
- Selector apply responses expose `auto_transition` and `effective_route` separately: logical server IDs and exact runtime selector targets describe the auto selector, while effective-route before/after describes the actual runtime target. `auto_transition.changed` is true/false only from known selector readbacks (or explicit no-op/dry-run); missing readback remains unconfirmed. Successful provenance is persisted only after the requested logical server uniquely matches the observed auto selector target.
- Explicit Xray Settings inventory advertises `supported_admin_modes` and `mode_support_state`; new per-client choices are VPN and Disabled. Existing Direct remains explicitly routed Direct, legacy Selective is blocked per client and marked unsupported, and neither stored value is rewritten during reads or ordinary profile reconciliation. A synthetic subscription-profile mode action updates the exact account/client enabled flags without hard deletion; explicit group VPN also changes current member intent to VPN.
- `/api/v2/reconcile` exposes a shared read-only reconcile snapshot for modules, subjects, Xray bindings, routing, VPN adapter health, and watchdog. It compares intent, execution/apply state, runtime observation, and projection state without repair and without changing database or runtime state.
- `/api/v2/diagnose` exposes a unified read-only diagnostic report built from state projection, reconcile, typed events, and SQLite schema/integrity checks. It returns `status`, `summary`, `sections`, `problems`, and `generated_at` without repair or runtime writes. User-facing sections are Database, Routing, VPN, Clients and sources, External integrations, and Watchdog; diagnostic-only events and legacy module reconcile details remain technical summary and do not degrade overall health by themselves. Legacy SQLite foreign-key history issues and optional external integrations can remain section-level `warning` with `details.overall_impact=false`; they do not drive overall health unless a runtime-impacting failure is also confirmed. Subject diagnostics count stale/drift only for user-impacting clients and external sources; host/docker/router compatibility inventory staleness is retained as technical detail. Watchdog failover cooldown/manual-selection suppression is presented as `warning` unless dataplane failure is confirmed.
- `GET /api/v2/diagnose?view=summary` is a compact current-state UI projection (`status`, `generated_at`, `sections` with stable `reason_code`); it skips event-history reads. The default full view and CLI retain their existing contract. If diagnose is unavailable, the UI shows unconfirmed `unknown` rather than deriving health from old logs.
- Diagnostic event history includes separately labeled recent technical failures and resolved/recovery events plus coverage/source metadata. It is historical evidence only and does not override current status. Event details/messages are recursively sanitized in storage and read projections; clients can use `X-Request-ID` and receive the accepted/generated ID in the response header.
- In database diagnostics, schema/integrity failure is `failed`; legacy foreign-key/history cleanup without confirmed runtime impact is `warning`.
- `/api/v2/events/recent` exposes the new read-only events view with `audit`, `operational`, `diagnostic`, and aggregation summary. It adapts legacy `operational_logs` without migration and hides diagnostic/noise events from the new operational list.
- `GET /api/v2/events/recent?view=summary` preserves every event ID, time, code, and structured correlation/error context while omitting bulky provider details and aggregate summary. The default full view is unchanged. The UI journal shows separate events with absolute timestamps.
- Typed events require a non-empty `event_code` alongside category, severity, component, schema version, and correlation context. Legacy writers and stored rows remain readable through an explicitly marked `event_type` compatibility code; no database migration is required. API error responses use stable codes and safe messages, while SQLite exception text and local filesystem paths remain internal.
- Event UI titles resolve by event code, then structured reason/error code. Legacy text translation remains available only for events marked as compatibility projections. Core event ownership covers health, routing, recovery, lifecycle, and configuration; future Mihomo, Xray, and Tailscale event codes belong to their module namespaces.
- Terminology: `latency` is a measured duration; `probe` is the operation that gathers evidence; `health` is the interpreted outcome; `status` is the current projected state. A logical server is the user selection, a member is one concrete endpoint; `external client` is an ingress identity, while module/node identifiers remain machine-level names.
- `fwrouter reconcile check` uses the same read-only reconcile service and prints a short operational summary (`SYSTEM OK` or drift/stale/failed counts).
- `fwrouter diagnose` uses the same diagnostic report and prints a human-readable summary; `fwrouter diagnose --json` returns the same object as `GET /api/v2/diagnose`.
- `/api/v2/ui/whoami` returns the current LAN/external ingress subject by IP with `effective_state`, making it the lightweight source for `mode_source` and `effective_mode` in user UI.
- `DELETE /api/v2/subjects/{subject_id}/mode` clears a user mode override and returns the client to global mode inheritance; it does not change manual VPN server selection.
- Mutating endpoints may accept `requested_by` as opaque attribution for UI, CLI, scheduler, or external management clients. `external_client` requests must include enough `management_context` (`client_name`, `action`).
- `POST /api/v2/core/bypass/enable|disable` requires `confirm_apply=true`; bypass changes runtime/dataplane core state through a job, not through a direct synchronous toggle.
- `POST /api/v2/maintenance/cleanup` creates a `maintenance_cleanup` job; `dry_run=true` is the default.
- `POST /api/v2/subscription/refresh` creates a locked `subscription_refresh` job instead of doing the long refresh inside the HTTP request. A concurrent refresh returns the existing active job for polling. Failed/stale jobs are marked failed by normal jobs stale cleanup, which releases the `subscription_refresh` lock for the next request.
- Module DTOs expose `lifecycle_mode` (`none`, `managed`, `external`), `installed`, and `manageable_actions`. External integrations are probe-only; module lifecycle actions are not exposed through the generic modules API.
- `GET /api/v2/servers` returns real server inventory by default. The Xray-only virtual target `virtual:xray:vpn-auto` is included only when `include_virtual_xray_vpn_auto=true`; it must not be saved into the normal Mihomo `vpn-auto` membership.
- Settings external clients use domain-level `/s/{name}` links. `POST /api/v2/xray/clients` remains the compatibility write adapter, but a link suffix create also creates the subscription profile identity and materializes profile nodes. Settings deletes enabled and disabled aggregates through the same `subscription-account:<account_id>` reference, while legacy callers may continue to use a token/slug. The backend validates account references before queuing the profile delete job.

### Health and Journal details — 2026-09-29

`GET /api/v2/events/{event_id}` reads one retained event by exact identity and returns `{event_id, found, event}`. Journal and Diagnostics lists retain compact summary reads; advanced disclosure lazily reads a full event or `GET /api/v2/diagnose?view=full`. Full technical evidence remains separate from safe ordinary details. Event-time safe names are preferred; current-name legacy fallback is explicitly marked and missing history is not reconstructed. Caller-supplied actor attribution is unverified and distinct from event source. External unknown/expired evidence does not itself raise overall Health; fresh confirmed offline/missing remains a warning.
