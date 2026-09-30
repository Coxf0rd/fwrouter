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

## Lazy details — 2026-09-29

Settings uses `view=summary` for initial loading and polling. Opening advanced details lazily reads existing `view=full`, with request deduplication and a bounded cache. The full report retains its own generation time, entity evidence, problems, and separately marked history; its technical fields never become ordinary UI labels. Failure to load details does not replace confirmed summary health.

The summary includes safe `sections.subjects.affected_entities` labels alongside
the canonical `affected_entity_count`. Grouped non-impacting unknown observations
and their per-subject evidence remain in the lazy full report.
