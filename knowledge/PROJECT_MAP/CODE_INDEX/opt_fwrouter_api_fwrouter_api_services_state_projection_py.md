# `/opt/fwrouter-api/fwrouter_api/services/state_projection.py`

## Purpose

Read-only normalized state projection layer above the current legacy state
fields. It builds a common DTO with intent, execution, observation, reconcile
and projection sections without changing the database, writers, migrations or
existing UI read models.

## Important Functions

- `build_system_state_projection()`
- `build_module_state_projection()`
- `build_subject_state_projection()`
- `build_routing_state_projection()`
- `build_watchdog_state_projection()`
- `build_rules_state_projection()`
- `build_xray_state_projection()`
- `build_vpn_state_projection()`

## External Dependencies

- SQLite state tables: `modules`, `subjects`, `routing_global_state`, `watchdog_state`, `rules_state`, `rules_metadata`
- runtime observations: dataplane status, Mihomo health, Xray health, Xray bindings artifact
- existing subject effective policy and scoped-egress read helpers

## Runtime/Persistent State

Read-only. It avoids auto-ensure helpers when they can create rows; for
`watchdog_state` it uses a direct SELECT without INSERT.

## Nuances

- `apply_state=clean` is not treated as proof of applied runtime.
- Inactive/missing subjects project as `inactive`, not degraded.
- Legacy fields are preserved under `legacy.raw` for compatibility diagnostics.
