# `/opt/fwrouter-api/fwrouter_api/services/control_plane_transfer_source.py`

## Purpose

Resolves payload/file-based snapshot sources and lists snapshot files in the transfer directory.

## Important Functions

- `_resolve_transfer_snapshot_path(file_path)`
- `_load_snapshot_file(path)`
- `resolve_control_plane_snapshot_source(...)`
- `list_control_plane_snapshot_files()`

## Notes

- Path confinement to `state_dir/transfer` is a security boundary.
