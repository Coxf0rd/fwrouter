# `/opt/fwrouter-api/fwrouter_api/services/control_plane_transfer_plan.py`

## Purpose

Builds dry-run import plans with validation, runtime-normalization impact, scoped-egress diagnostics, and post-import expectations.

## Important Functions

- `_snapshot_bypass_state(...)`
- `_snapshot_active_override(...)`
- `_enriched_subjects_from_snapshot(...)`
- `plan_control_plane_import(...)`

## Notes

- Planning must not write SQLite or live dataplane state.
- With runtime normalization enabled, the plan must warn that a fresh Linux-side apply is required.
