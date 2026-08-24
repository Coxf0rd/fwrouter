# `/opt/fwrouter-api/fwrouter_api/services/ui_display_settings.py`

## Purpose

Compatibility facade for UI display settings and the Settings > Connections read-model. Constants/normalization live in `ui_display_settings_common.py`; persisted settings in `ui_display_settings_store.py`; display-system assembly in `ui_display_settings_display.py`; guides/readiness in `ui_display_settings_guides.py`; custom external connection write/contract API in `ui_display_settings_external.py`.

## Important Functions

- `_display_systems(...)`
- `_system_visible(...)`
- `external_connection_identity(...)`
- `custom_external_system_by_id(...)`
- `external_connection_contract(...)`
- `preview_custom_external_connection(...)`
- `upsert_custom_external_connection(...)`
- `delete_custom_external_connection(...)`

## Notes

- Keep this file as a thin re-export facade.
- UI-visible labels/reasons should come from `ui_text.py`, not local formatter strings.
