# `/opt/fwrouter-api/fwrouter_api/routes/state.py`

## Purpose

GET-only API for read-only normalized state projection.

## Important Endpoints

- `GET /api/v2/state/system`
- `GET /api/v2/state/modules`
- `GET /api/v2/state/subjects`
- `GET /api/v2/state/subjects/{subject_id}`
- `GET /api/v2/state/routing`
- `GET /api/v2/state/watchdog`
- `GET /api/v2/state/rules`
- `GET /api/v2/state/xray`
- `GET /api/v2/state/vpn`

## External Dependencies

- `services/state_projection.py`
- common `ApiResponse`

## Runtime/Persistent State

Read-only. It does not change the database, runtime, UI read models or legacy
API responses.
