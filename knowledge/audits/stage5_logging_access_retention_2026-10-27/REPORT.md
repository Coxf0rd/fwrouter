# Stage 5 — logging access and retention audit

Base source: `7bf1a79ac8fe28b71def9f06e75a1c7e1c1cd1d2`, PR55 squash tree
`a68b1b23ada5c13d3028c1dc5b02394693a1b91e`. Qualified actual-main run
[40519112883](https://github.com/Coxf0rd/fwrouter/actions/runs/40519112883), attempt1:
16/16 mandatory jobs SUCCESS, exact source/tree confirmed; main evidence bundle
hash, bounded extraction and trusted offline verification PASS. These are source
and isolated CI facts, not installed-host observations.

## Read-path matrix — VERIFIED source

| Reader | Existing access | Data / protection | Residual boundary |
|---|---|---|---|
| `/events/recent` full/summary, `/events/{id}` | No API read admission | Typed audit/operational/diagnostic objects, bounded recent/by-ID; new reserved identity stripped | Full is a projection choice, not authority; old operation/actor/correlation can remain visible |
| `/logs/operational`, `/logs/technical` | No API read admission; ui_only is presentation | Existing sanitized compatibility records, coalescing/filtering and identity redaction | Raw technical diagnostics need trusted deployment access; no promise every free-text secret is recognized |
| `/diagnose` summary/full, `fwrouter diagnose` | HTTP API-backed, no authenticated CLI principal | Existing diagnostic report/history and identity-safe projection | Loopback is not credential identity; external access UNKNOWN |
| `/jobs`, `/jobs/{id}`, `/rules/jobs/{id}` and conflict objects | No uniform read admission | Existing input/result/context, recursive reserved-identity redaction | Caller requested_by and arbitrary historical job diagnostics are not privileged facts |
| UI workspace/router summaries and Settings event details | Same API paths | Existing localized summary/detail behavior retained | UI admin view is not authorization; proxy/TLS/custody unqualified |
| Control-plane export | Existing transfer owner | Declared control-plane tables exclude events/logs/jobs/host registry | Not a public raw log/backup download; do not infer protection of other exported data |
| Direct SQLite/JSONL/backups; service/proxy journals | Host administration | Existing file custody, source UMask and SQLite private modes | Actual host ACLs, backup readers, journald and proxy rotation NOT VERIFIED |

References: `routes/events.py`, `routes/logs.py`, `routes/diagnose.py`,
`routes/jobs.py`, `routes/rules.py`; `schemas.py:ApiResponse`,
`services/api_client_attribution.py:redact_http_attribution`,
`services/ui_state_summary.py`, `services/diagnostics.py`,
`services/control_plane_transfer_common.py:CONTROL_PLANE_TABLES`,
`cli.py` diagnose path. No deployed headers/proxy assumptions were used.

## Confirmed retention defects and bounded remedy

1. Before this package, `maintenance.py:run_control_plane_maintenance` selects and
   deletes every old `operational_logs` row at the same three-day cutoff. Typed
   audit and security denial/conflict data share that table. This silently applies
   operational expiry to audit/security. Remedy: classify under the existing
   owner and HOLD protected evidence pending an approved distinct retention term.
2. `logs_retention.py:_cleanup_jsonl_file` formerly reads into a temporary file
   and replaces the original without coordination with appenders. An append after
   reader EOF and before replace is lost. Remedy: one cooperating per-file lease
   across the existing append, cleanup and startup scrub paths, private publication
   and deterministic concurrency/failure regressions. This changes no domain
   intent, routing, registry, native generation or recovery authority.
3. Existing unauthenticated log readers are a source access gap, not proof of an
   external breach or observed secret leak. The package preserves the richer
   identity's host-admin-only boundary rather than inventing unqualified read
   authority or disabling current UI behavior.

See [access/retention contract](../../LOGGING_ACCESS_AND_RETENTION.md) for the
preferred extended read model, temporary HOLD semantics and production gates.

## Safe capacity measurement

Three fresh isolated schema27 SQLite states were used with real event writers.
Each emitted 960 synthetic records:300typed audit,60security denial,300ordinary
operational and300technical. Typed audit is SQL-only; operational/security also
mirror JSONL. Each sample therefore has660SQL rows and660JSONL lines, not1320
unique events. No live data, credentials or services were used.

Baseline payload sizes: audit details988bytes/event; security details721bytes
plus1143bytes/JSONL line; operational details693bytes plus1095bytes/JSONL line;
technical1282bytes/JSONL line. Three samples each produced781460JSONL bytes; SQLite
allocated approximately1.35MB including existing schema/index overhead. This is a
measured synthetic envelope, not a production event mix or physical SSD-write
measurement. Controlled emission was193–202audit/s,154–203security/s,
187–202operational/s and1362–1616technical/s. These are generator throughput,
NOT observed events/day. The final FD-anchored candidate produced the same payload sizes and line/row
counts. In the three-sample controlled run, median emission times were:

| Class / emitted count | Baseline seconds | Candidate seconds |
|---|---:|---:|
| audit / 300 | 1.510 | 1.544 |
| security / 60 | 0.304 | 0.421 |
| operational / 300 | 1.530 | 2.155 |
| technical / 300 | 0.220 | 0.993 |

File coordination has a measurable synthetic emission cost; it is a correctness
fix, not a throughput optimization. Test isolation audits every filesystem call
and may amplify that cost. These three sequential samples are not a production
latency, CPU/RSS, disk-write or scalability qualification. A representative live
size/rate and lock-contention qualification remains required before rollout.
Exact source hashes, per-sample measurements and limitations are retained in the
private handoff evidence; no production traffic rate is inferred.


Recommended production policy: retain ordinary operational3days and technical14days
provisionally as existing behavior; do not use either as the audit/security term.
HOLD audit/security until an explicit supported recovery/incident horizon and byte
budget are qualified. Required model is `events/day × bytes/event × days`, with
SQL/index/WAL, JSONL duplication and backups measured separately. No production
arrival-rate observation exists in this source-only task, so a fixed audit/security
number cannot honestly be called qualified. Job expiry may remove terminal details
before an audit reference expires; this remains a distinct evidence-lifecycle gate.

## CI performance audit

See [timing analysis](CI_PERFORMANCE_AUDIT.md). The current main's two separate
~175s silent step-transition intervals explain most of the wall-time regression;
neither is test execution or measured artifact transfer. Underlying platform cause
is UNKNOWN. Preserve the current graph pending repeated observations and actual
per-node/cohort timings. No workflow edits are included.

## Qualification status

Source implementation/tests, independent QA and exact-HEAD mandatory CI results
are recorded in the final handoff. Audit review alone does not qualify a runtime,
production retention/access policy, L7 or deployment.
