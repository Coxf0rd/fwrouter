# `/opt/fwrouter-api/fwrouter_api/cli.py`

## Purpose

Console entrypoint `fwrouter` for read-only operational commands.

## Important Commands

- `fwrouter reconcile check`
  Reads the shared reconcile service and prints a short summary such as
  `SYSTEM OK`, `XRay: checked/drift/stale`, and `Routing: rules/dataplane`.
- `fwrouter diagnose`
  Reads the shared diagnostic report and prints a human-readable system state.
  `fwrouter diagnose --json` returns the same object as `GET /api/v2/diagnose`.

## Runtime/Persistent State

The commands only read backend state/probes through `services.reconcile` and
`services.diagnostics`; they do not change the database or runtime.
