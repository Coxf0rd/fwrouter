# `/opt/fwrouter-api/fwrouter_api_maintenance.py`

## Purpose

Systemd maintenance CLI entrypoint. It dispatches schema inspection, database rebuild, or control-plane cleanup without running the API startup/bootstrap lifecycle first.

## Admission and side effects

- `schema-check` calls `inspect_existing_database_schema()` through SQLite `mode=ro`, prints the schema summary, and exits nonzero when the database is unavailable or incompatible. It does not initialize or migrate the DB.
- `cleanup` and `cleanup --dry-run` first perform that same read-only schema admission. Only a compatible existing DB proceeds to `run_control_plane_maintenance()`. Real cleanup retains its documented maintenance effects, including bounded member probes and applying an expired global fixed-server transition when needed. Dry-run suppresses cleanup writes and runtime apply.
- `rebuild-db` dispatches directly to `rebuild_control_plane_database()`, which owns snapshot resolution and selection-fence preflight. Do not add a general bootstrap ahead of those gates.

The entrypoint must not trigger API startup reconciliation, stale-job cleanup, DNS reconcile, or schema migration merely because a maintenance command was invoked. Initialization/migrations remain owned by installer/API startup.

## Runtime and callers

Called by `fwrouter-maintenance.service` and operator CLI invocations. The separate `fwrouter-jobs-retention-dry-run.timer` calls the API jobs endpoint as an operational canary; it does not invoke this CLI.

## Guardrails

- Keep schema admission read-only and before maintenance effects.
- Preserve cleanup and rebuild ownership of their existing apply, snapshot, and revision fences.
- Do not use `schema-check` or `cleanup --dry-run` as a substitute for the explicit installer/API initialization lifecycle.
- Keep FWRouter Core authoritative for routing and selector state.
