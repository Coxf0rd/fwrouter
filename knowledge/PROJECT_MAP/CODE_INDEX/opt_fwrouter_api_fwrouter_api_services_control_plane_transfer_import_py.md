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
Control-plane import never accepts a foreign Core revision. It preserves and advances the local monotonic fence transactionally and normalizes imported enabled Core to pending.

The public import service validates the snapshot before database initialization,
then requires the request-scoped maintenance admission before acquiring the
Xray writer guard. The API route verifies the fixed root-owned bearer verifier
before transfer-path resolution. See
`docs/CONTROL_PLANE_MAINTENANCE_ADMISSION.md`. This does not qualify atomic
recovery across rules files and SQLite; partial filesystem publication remains
an explicit failure limitation.

## Restore extension under qualification

Import now prepares private rules files and publishes their existing path columns
with all imported rows in one SQLite transaction. The owner holds its existing
Xray guard and an immediate SQLite writer transaction; active jobs conflict.
Local import receipts support idempotent retry, cannot be restored as foreign
authority, and are excluded from exports. Native generation authority stays
local and is invalidated by the existing subscription owner after import.
See `docs/CONTROL_PLANE_RESTORE_CONTRACT.md`; historical admission-only PASS does
not qualify this extended implementation.
