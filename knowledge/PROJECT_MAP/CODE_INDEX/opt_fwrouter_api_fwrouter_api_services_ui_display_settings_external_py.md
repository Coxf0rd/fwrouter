# `/opt/fwrouter-api/fwrouter_api/services/ui_display_settings_external.py`

## Purpose

Write, preview, and contract API for custom external connections in UI display settings.

## Important Functions

- `preview_custom_external_connection(...)`
- `upsert_custom_external_connection(...)`
- `delete_custom_external_connection(...)`
- `external_connection_contract(...)`
- `_normalize_external_connection_input(...)`

## Notes

- `connection_id`, `connection_type`, and `replacement_target` are immutable when patching existing records.
- `system_id` is secondary display/compatibility metadata; normal API paths address records by `connection_id`.
