# `/opt/fwrouter-api/fwrouter_api/services/ui_display_settings_common.py`

## Purpose

Shared constants, validation error class, JSON helpers, system-id normalization, identity helpers, and external connection normalizers.

## Important Functions

- `_slugify_system_id(...)`
- `_normalize_custom_external_systems(...)`
- `external_connection_identity(...)`
- `_normalize_external_*`

## Notes

- Endpoint/capability/collector allowlists live here and should be changed together with validation tests.
