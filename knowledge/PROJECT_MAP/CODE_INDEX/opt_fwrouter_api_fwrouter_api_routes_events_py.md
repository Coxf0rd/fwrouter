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

## Approved Health / Journal implementation contract — 2026-09-29

- `GET /api/v2/events/{event_id}` performs an exact read across existing SQLite and technical JSONL records, including retained events outside the recent-list window. It returns `{event_id, found, event}`; a missing retained record has `found=false` and `event=null`. Full details use the existing sanitizer and legacy deterministic event IDs.
- Journal initial loading remains `view=summary`. Full event evidence is loaded only on advanced disclosure and never replaces the safe ordinary projection.
- Safe event-time object labels take precedence. `entity_label_source` marks `event_snapshot`, `current`, or `missing`; legacy current-name lookups are not historical snapshots. Missing old labels/values are not reconstructed. Selection/assignment transitions use safe `server_label` snapshots; identity references remain technical.
- For legacy `vpn_auto_server_switched` rows that have no envelope actor, summary may promote `details.requested_by` through `safe_actor_identifier`; the raw field stays out of details. Existing safe previous/new server labels, reason, result, and source remain in the bounded summary allowlist.
