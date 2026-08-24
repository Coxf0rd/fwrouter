# `/opt/fwrouter-api/fwrouter_api/services/control_plane_transfer.py`

## Purpose

Compatibility facade for the control-plane snapshot transfer layer. Shared constants/path helpers live in `control_plane_transfer_common.py`; export, source resolution, validation, dry-run planning, and import/writeback live in focused `control_plane_transfer_*` modules.

## Important Functions

- `export_control_plane_snapshot(...)`
- `resolve_control_plane_snapshot_source(...)`
- `list_control_plane_snapshot_files()`
- `validate_control_plane_snapshot(...)`
- `plan_control_plane_import(...)`
- `import_control_plane_snapshot(...)`

## Runtime State

- Snapshot files under `/var/lib/fwrouter-v2/transfer`
- Reads/writes FWRouter SQLite control-plane tables during import/export

## Notes

- Keep this file as a thin re-export facade.
- Do not weaken transfer-directory path confinement or secret redaction behavior.
