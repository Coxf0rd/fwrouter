# `/opt/fwrouter-api/fwrouter_api/services/control_plane_transfer_common.py`

## Purpose

Shared constants, DB fetch helpers, JSON/path helpers, snapshot-state helpers, and subject-detail table mapping for control-plane transfer.

## Important Functions

- `_transfer_dir()`
- `_snapshot_file_path()`
- `_detail_table_for_subject_type(...)`
- `_state_from_snapshot(...)`
- `_insert_rows(...)`

## Notes

- `CONTROL_PLANE_TABLES` is the full table replacement contract for import; update export/import/validation together when changing it.
