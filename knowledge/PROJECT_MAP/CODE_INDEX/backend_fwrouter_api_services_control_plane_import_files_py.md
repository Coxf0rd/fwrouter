# `backend/fwrouter_api/services/control_plane_import_files.py`

`prepare_rules_files()` prepares all eight import-owned rules artifacts in a
private unique directory, validates readback and fsyncs dependencies. It never
writes currently selected paths. `control_plane_transfer_import` activates the
returned paths in the existing SQLite transaction with imported rows and local
idempotency receipts. It removes a controlled failed preparation only; crash
orphans are not trusted or auto-adopted. This helper is not a runtime/recovery
owner and does not claim activation of native routing.

See [restore contract](../../../docs/CONTROL_PLANE_RESTORE_CONTRACT.md),
`backend/tests/test_import_publication.py`, and the owning importer index.
