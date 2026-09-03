# `/opt/fwrouter-api/fwrouter_api/services/state_projection_types.py`

## Purpose

Pydantic DTO types for normalized state projection.

## Important Classes

- `StateIntentDTO`
- `StateExecutionDTO`
- `StateObservationDTO`
- `StateReconcileDTO`
- `StateProjectionDTO`
- `EntityStateProjectionDTO`

## Runtime/Persistent State

Does not read or write state.

## Nuances

The DTO shape is intentionally generic and shared by modules, subjects,
routing, watchdog, rules, Xray and VPN runtime projections.
