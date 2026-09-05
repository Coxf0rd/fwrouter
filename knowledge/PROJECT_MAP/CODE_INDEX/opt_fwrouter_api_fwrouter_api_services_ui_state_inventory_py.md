# `/opt/fwrouter-api/fwrouter_api/services/ui_state_inventory.py`

## Purpose

Owns settings inventory DTOs.

## Main Responsibilities

- Build role-filtered inventory rows for local clients, external clients, network sources, services, and infrastructure entries.
- Preserve legacy role-based `kind` and `inventory_role` for API filters while exposing derived `domain_category` and keeping concrete adapter data in `implementation_kind` / `implementation_label`.
- `live_observations=False` is the fast presentation path: it returns persistent inventory without blocking on provider acquisition and uses only already warm cached observations.
- Add traffic panel metrics, activity reasons, visibility fields, and mode summaries.
- Group Xray subscription profile subjects for settings inventory.

## Runtime Impact

Reads SQLite subjects, traffic, subscription, routing global state, and active user overrides. It does not apply runtime changes.

## Guardrails

- Keep settings inventory lightweight and free of live dataplane probes.
- Do not block first Settings render on live provider observations unless the caller asks for them.
- Preserve `display_system_id` for external network rows so UI visibility stays source-specific.
- Do not map external-client enabled/disabled state into policy routing modes.
