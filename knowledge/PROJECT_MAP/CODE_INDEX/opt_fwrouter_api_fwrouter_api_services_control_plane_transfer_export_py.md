# `/opt/fwrouter-api/fwrouter_api/services/control_plane_transfer_export.py`

## Purpose

Builds control-plane snapshots from SQLite state, rules artifacts, subjects, subscription state, and server inventory.

## Important Functions

- `_export_subjects()`
- `_redact_subscription_state(...)`
- `_redact_custom_https_proxy_rows(...)`
- `_export_rules_bundle()`
- `export_control_plane_snapshot(...)`

## Notes

- `include_secrets=false` must redact subscription URLs and custom HTTPS proxy credentials.
- File export writes only through the transfer directory.
