# `/opt/fwrouter-api/fwrouter_api_services_logs.py`

## Purpose

Centralized legacy operational/technical log read/write helpers.

## Important Functions

- `write_operational_log(...)`
- `write_technical_log(...)`
- `log_event(...)`
  Compatibility alias imported from `services.events` for the old generic
  event call shape.
- `list_operational_logs(...)`
- `list_technical_logs(...)`
- shared envelope, sanitization, truncation and event-context helpers are in `services/event_contract.py`

## External Dependencies

- SQLite `operational_logs`
- JSONL files under `/var/log/fwrouter/operational` and
  `/var/log/fwrouter/technical`
- `core/config.py`

## Runtime/Persistent State

- operational events are written to SQLite and JSONL
- technical events are written to JSONL
- in-memory dedupe suppresses repeated events by `(component,event_type,dedupe_key)`

## Notes

- Dedupe cooldown only applies inside the current backend process.
- Details are recursively sanitized before persistence/JSONL and on read. Oversized payloads keep the envelope/error/correlation fields plus hash/size/count metadata.
- Operational and technical writers share event IDs and workflow/causation context when callers provide them.
- The typed events layer lives in `services/events.py`; this file preserves the
  legacy operational/technical log API.
