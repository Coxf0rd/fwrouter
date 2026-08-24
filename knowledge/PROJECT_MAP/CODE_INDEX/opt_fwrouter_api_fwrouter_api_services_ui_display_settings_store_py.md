# `/opt/fwrouter-api/fwrouter_api/services/ui_display_settings_store.py`

## Purpose

Reads/writes persisted UI display settings row `ui.admin_client_display.v1` and normalizes response state.

## Important Functions

- `_load_display_settings_raw()`
- `_save_display_settings_raw(...)`
- `_normalized_display_settings_for_response(...)`
- `_normalize_system_visibility(...)`
- `_system_visible(...)`
- `custom_external_system_by_id(...)`

## Notes

- Saving settings clears the live probe cache.
- Visibility is stored by canonical system id.
