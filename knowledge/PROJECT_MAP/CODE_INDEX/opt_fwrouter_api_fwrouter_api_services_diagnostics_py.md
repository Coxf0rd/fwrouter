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

- Severity model: `healthy`, `warning`, `degraded`, `failed`, `inactive`, `disabled`, `unknown`.
- Missing or empty projections and `reconcile=unknown` remain `unknown` and do not create `DiagnosticProblem`; explicit healthy/in-sync evidence stays healthy, while confirmed stale/drift states still affect severity.
- Health UI localizes `reason_code` and selects administrative actions by stable code; unknown reasons do not fall back to a generic diagnostics instruction.
- An Xray pending DB apply marker with a runtime-confirmed binding is warning,
  not failed.
- Reconcile drift becomes a correlated diagnostic problem with source such as
  `{entity_type}_reconcile`.
- Stale observations for enabled explicit Xray/VLESS clients are exposed as
  `unknown` with `overall_impact=false` unless projection/reconcile evidence
  confirms failure or drift. Their `stale_unconfirmed_count` remains available
  in the subjects section; this does not use traffic recency as proof of health.
- Stale active LAN observations explicitly marked as `dnsmasq_leases` are also
  retained as `SUBJECT_OBSERVATION_STALE` evidence with `unknown` severity and
  no confirmed overall impact, unless execution/reconcile reports failure,
  drift, or a pending intent.
- `EXTERNAL_INTEGRATION_OBSERVATION_MISSING` means no successful interval
  collector run has been recorded; it is not cleared by an unrelated provider
  status probe. Its optional-integration classification remains non-impacting.
- Xray missing-binding counts include identities from reconcile
  `details.missing_subject_ids`; wording states that an applied binding is
  missing and traffic impact is unconfirmed.
