# `/opt/fwrouter-api/fwrouter_api/services/control_plane_transfer_import.py`

## Purpose

Applies validated control-plane snapshots into SQLite and rules artifact files.

## Important Functions

- `_write_rules_files_from_snapshot(...)`
- `_normalized_module_row(...)`
- `_normalized_subject_row(...)`
- `_normalized_rules_state(...)`
- `import_control_plane_snapshot(...)`

## Notes

- Import replaces managed control-plane tables inside a DB transaction.
- Runtime normalization resets apply/runtime state to pending/not_configured instead of treating live dataplane as applied.
- Legacy subscription snapshots with `subscription_state.url` but no `metadata.subscriptions.items` are normalized into the backend-side subscription registry while preserving the compatibility URL.
- Successful import writes `control_plane_snapshot_imported` operational log.
