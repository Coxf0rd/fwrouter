# 0001: Use FastAPI As Control Plane

## Status

Accepted.

## Context

FWRouter needs a backend that stores intent/state, exposes API routes, manages apply jobs, and runs startup recovery.

## Decision

Use the FastAPI backend `fwrouter-api` as the single control-plane entry point.

## Consequences

- One API surface for UI, CLI-style operations, jobs, and diagnostics.
- Startup lifecycle hooks can run bootstrap and recovery logic.
- Backend restarts now affect recovery semantics.
- If startup hooks fail, boot persistence fails together with the API.

## Related Files

- `/opt/fwrouter-api/fwrouter_api/main.py`
- `/opt/fwrouter-api/fwrouter_api/services/bootstrap.py`
- `/etc/systemd/system/fwrouter-api.service`
