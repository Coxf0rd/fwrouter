# `/opt/fwrouter-api/fwrouter_api/services/control_plane_transfer_validation.py`

## Purpose

Validates control-plane snapshot structure and compatibility before dry-run planning or import.

## Important Functions

- `validate_control_plane_snapshot(...)`

## Notes

- Unsupported `snapshot_version` is a hard error.
- Redacted secrets and missing subject details are warnings when the base structure remains usable.
