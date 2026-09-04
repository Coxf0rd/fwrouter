# `/opt/fwrouter-api/fwrouter_api/services/diagnostics.py`

## Purpose

Unified read-only diagnostic framework for answering "what is wrong right now,
and why".

## Important Classes And Functions

- `DiagnosticReport`
  DTO with `status`, `summary`, `sections`, `problems`, and `generated_at`.
- `DiagnosticProblem`
  Correlated problem DTO with `entity_type`, `entity_id`, `severity`, `reason`,
  `source`, `suggested_investigation`, and `details`.
- `build_diagnostic_report()`
  Builds the report from SQLite schema/integrity checks, state projection, the
  reconcile framework, and typed events summary.
- `format_diagnostic_report()`
  Human-readable output for `fwrouter diagnose`.

## Runtime / Persistent State

Only reads SQLite through schema/integrity PRAGMAs and existing read-only
projection/reconcile/events helpers. It does not run repair and does not change
runtime areas: Tailscale, SSH, ACLs, firewall/nftables, routing, dnsmasq,
Mihomo, Xray, or systemd network units.

## Notes

- Severity model: `ok`, `warning`, `degraded`, `failed`.
- An Xray pending DB apply marker with a runtime-confirmed binding is warning,
  not failed.
- Reconcile drift becomes a correlated diagnostic problem with source such as
  `{entity_type}_reconcile`.
