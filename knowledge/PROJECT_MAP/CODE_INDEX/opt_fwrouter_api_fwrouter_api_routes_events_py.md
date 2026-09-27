# `/opt/fwrouter-api/fwrouter_api/routes/events.py`

## Purpose

Publishes the read-only endpoint `GET /api/v2/events/recent`.

## Important Functions

- `list_recent_events_endpoint()`
  Returns `{audit, operational, diagnostic, summary}` with `type`, `severity`,
  `entity_id`, `since`, and `limit` filters.
- Compact event rows expose a bounded safe `entity_label` only when one is
  already present in event fields/details. Technical IDs remain in advanced
  details; ID-, credential-, and URL-like labels are omitted.

## Runtime/Persistent State

Reads the typed events view through `services.events` only; it does not trigger
repair or change runtime.
