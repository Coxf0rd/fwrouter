# `/opt/fwrouter-api/fwrouter_api/routes/reconcile.py`

## Purpose

Publishes the read-only endpoint `GET /api/v2/reconcile`.

## Important Functions

- `get_reconcile_endpoint()`
  Returns `{entities, summary}` from
  `services.reconcile.build_reconcile_response()`.

## Runtime/Persistent State

Does not write state or trigger repair. The endpoint is diagnostic-only for
drift/stale/failed between persisted intent, execution state, runtime
observation, and projection state.
