# `/opt/fwrouter-api/fwrouter_api/services/server_inventory.py`

## Purpose

Owns server inventory serialization and lookup.

## Main Responsibilities

- Convert SQLite server rows into API dictionaries.
- List and filter servers by inventory state, provider, routing preferences, and search text.
- Return one server by `server_id`.
- Sync Mihomo-discovered servers into SQLite inventory rows.
- Project canonical ping state for UI consumers: top-level `ping` fields use
  the manual/presentation lane, with nested `manual` and `background` details.

## Runtime Impact

Writes inventory and preference bootstrap rows during Mihomo sync. Does not apply
dataplane or change active selectors directly.

## Guardrails

- Keep row-to-dict output compatible with UI and API consumers.
- Do not expose background/watchdog failures as the user's last manual ping
  value; preserve the source split introduced in schema 15.
- Treat inventory sync as discovery state, not as global routing intent.
- Preserve JSON parsing defaults for malformed optional metadata.
