# `/opt/fwrouter-api/fwrouter_api/cli.py`

## Purpose

Console entrypoint `fwrouter` for read-only operational commands.

## Important Commands

- `fwrouter reconcile check`
  Reads the shared reconcile service and prints a short summary such as
  `SYSTEM OK`, `XRay: checked/drift/stale`, and `Routing: rules/dataplane`.

## Runtime/Persistent State

The command only reads backend state/probes through `services.reconcile` and
does not change the database or runtime.
