# `/opt/fwrouter-api/fwrouter_api/services/ui_state_settings.py`

## Purpose

Owns persisted UI display settings.

## Main Responsibilities

- Load and save `UI_DISPLAY_SETTINGS_KEY` from the SQLite `settings` table.
- Normalize system visibility, custom external systems, hidden subjects, and traffic panel preferences.
- Clear UI read-model cache and trigger lightweight prewarm after settings changes.

## Runtime Impact

Writes display settings to SQLite and invalidates short TTL read-model caches.

## Guardrails

- Custom external systems are display/registration records only.
- Keep `system_visibility` role-based; do not introduce implementation-specific visibility contracts.
- Normalize traffic metric preferences through shared UI state helpers.
