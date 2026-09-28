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
- Summary view permits `old_status`/`new_status` only for known member-health
  transition codes (or the explicit legacy transition event type) and fixed
  health enum values. Other event fields keep the existing bounded allowlist.
- Category filtering uses canonical `classify_event` before the database limit,
  preventing recent routine rows from starving category-specific audit views.
  Compact summaries include bounded `changed_fields` and safe batched entity
  labels; member event-time labels are preferred, with bounded historical lookup.
  Historical lookup may reflect a current name after rename or be unavailable
  after deletion; membership counts remain explicit when some labels cannot be
  resolved.

## Runtime/Persistent State

Reads the typed events view through `services.events` only; it does not trigger
repair or change runtime.
