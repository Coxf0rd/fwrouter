# `/opt/fwrouter-api/fwrouter_api/routes/diagnose.py`

## Purpose

Publishes the read-only `GET /api/v2/diagnose` endpoint.

## Important Functions

- `get_diagnose_endpoint()`
  Returns `{status, summary, sections, problems, generated_at}` from
  `services.diagnostics.build_diagnostic_report()`.

## Runtime / Persistent State

The endpoint does not write state, does not run apply/repair, and does not
perform runtime changes. It uses the same report object as
`fwrouter diagnose --json`.
