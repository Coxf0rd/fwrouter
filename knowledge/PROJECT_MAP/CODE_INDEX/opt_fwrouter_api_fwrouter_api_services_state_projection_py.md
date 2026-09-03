# `/opt/fwrouter-api/fwrouter_api/services/state_projection.py`

## Purpose

Read-only normalized state projection layer above the current legacy state
fields. It builds a common DTO with intent, execution, observation, reconcile
and projection sections without changing the database, writers, migrations or
existing UI read models.

## Important Functions

- `compute_reconcile_state()`
- `compute_staleness()`
- `compute_health_level()`
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
- read-only routing snapshot and override SELECTs, avoiding helpers that can normalize expired state as a write side effect

## Runtime/Persistent State

Read-only. It avoids auto-ensure helpers when they can create rows; for
`watchdog_state` and routing state it uses direct SELECTs without INSERT/UPDATE.

## Nuances

- `apply_state=clean` is not treated as proof of applied runtime.
- Module projection prefers factual probes for core/dataplane, Mihomo, Xray and watchdog when available; DB module state remains intent/execution metadata.
- Subject projection exposes normalized `identity`, `effective` and `reason` blocks while preserving legacy raw fields.
- Routing projection exposes global mode, selective rule summary, direct exception counts, forced-VPN binding context and dataplane enforcement evidence.
- Xray projection treats an active client with an applied runtime binding as reconciled even if a legacy DB override apply marker is stale/pending.
- VPN projection reports Mihomo as an egress adapter only; FWRouter routing state remains the policy source of truth.
- Inactive/missing subjects project as `inactive`, not degraded.
- Legacy fields are preserved under `legacy.raw` for compatibility diagnostics.
