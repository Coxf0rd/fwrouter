# `/opt/fwrouter-api/fwrouter_api/services/server_state.py`

## Purpose

Owns persisted global routing state helpers for server selection.

## Main Responsibilities

- Ensure the canonical `routing_global_state` row exists.
- Read global routing state and clear expired fixed-server TTL state.
- Expire global fixed-server mode back to auto when the TTL is reached.
- Update global mode, selective default, and drift status fields.

## Runtime Impact

Writes SQLite intent/state. When requested, fixed-server expiry delegates runtime
apply back to the global auto server flow.

## Guardrails

- Do not treat live dataplane state as source of truth.
- Keep desired/applied fields distinct.
- Avoid top-level imports that recreate selection/state circular dependencies.
