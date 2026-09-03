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
- `create_event_context()`
  Builds first-class correlation fields: `request_id`, `job_id`, `apply_id`,
  `entity_id`, `server_id`, `connection_id`.
- `classify_event()`
  Classifies explicit and legacy `event_type` values as `audit`,
  `operational`, or `diagnostic`.
- `log_event()`
  Compatibility adapter for the old call shape.
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
