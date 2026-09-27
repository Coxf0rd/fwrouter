# `/opt/fwrouter-api/fwrouter_api/services/events.py`

## Purpose

First typed audit/operational/diagnostic events layer over the existing
`operational_logs` table and technical JSONL files without schema migration.

## Important Classes And Functions

- `AuditEvent`
  User/API actions and settings changes.
- `OperationalEvent`
  State transitions, failures, apply results, reconcile drift, and failover.
- `DiagnosticEvent`
  Probe/debug/raw runtime details; not included in the new operational journal.
- Typed envelope schema version is 2. All typed DTOs require a non-empty `event_code`; direct typed writers require
  one explicitly. `log_event()` remains the legacy adapter and marks a
  missing-code `event_type` fallback in `details.event_code_compatibility`.
  Historical rows are projected with the same marker and are not rewritten.
- Event envelope schema version, category, severity, component, and correlation
  fields remain first-class. Core codes cover health, routing, recovery,
  lifecycle, and configuration; future module-owned codes use `mihomo.*`,
  `xray.*`, or `tailscale.*` namespaces without changing Core settings.
- `create_event_context()`
  Builds first-class correlation fields: `request_id`, `job_id`, `apply_id`,
  `entity_id`, `server_id`, `connection_id`.
- `classify_event()`
  Classifies explicit and legacy `event_type` values as `audit`,
  `operational`, or `diagnostic`.
- `log_event()`
  Compatibility adapter for the old call shape.
- `write_audit_event()` writes INFO/audit records and can share the caller's
  SQLite connection so persistent mutations and their audit row commit or roll
  back together. Caller-supplied actors are short-identifier validated; values
  pass through the existing redactor. Legacy event types can be preserved.
  `admin.audit_event_missing` is an operational diagnostic for a committed
  intent without a matching atomic audit row, not an audit substitute.
- `list_recent_events()`, `summarize_events()`
  Read-only event selection and aggregates for API.

## Runtime/Persistent State

Write helpers use existing `operational_logs` for audit/operational events and
technical JSONL for diagnostics. The database schema is unchanged; runtime,
networking, Tailscale, SSH, firewall/nftables, routing, and service lifecycle
are untouched.

## Notes

Correlation fields are first-class in DTO/API. In storage they are compatibly
duplicated into `details_json` because separate columns are not added yet.
Watchdog healthy/no-traffic heartbeat and successful technical materialize
events are classified as diagnostic for the new operational journal.

## Rules/routing event catalog (2026-09-27)

The Core catalog includes stable audit codes for manual-rule draft/activation and global routing intent changes. UI translations resolve by `event_code` in both supported locales.
