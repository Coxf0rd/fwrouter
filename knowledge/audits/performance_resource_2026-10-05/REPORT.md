# Stage 4 — Performance & Resource Efficiency Audit

Date: 2026-10-05. Source baseline `0b24cd1`; deployed application baseline `7478b02` (subsequent source changes were documentation-only). Selected deployed/source files matched. Schema 24. **Audit only: no application fixes, deployment, restart, provider mutation, forced outage, refresh or apply.**

The detailed consolidated Russian report is [the local canonical audit](</решения/аудит/аудит №1 2026-10-05.md>). This English source-owned summary and the linked sanitized artifacts preserve the measured evidence in Git. Read the component limitations before interpreting sample statistics.

## Verdict and priorities

No P0 performance or routing issue was confirmed. The bounded audit is complete; missing attribution and long-window measurements remain explicit gates for subsequent measured fixes, not fabricated acceptance.

| Finding | Classification | Evidence / priority |
|---|---|---|
| Narrow inventory builds all-subject Health before role filtering | Confirmed over-broad admission | Cold router_core GET 2,483.8 ms; warm 11–26 ms. P1 latency candidate; exact internal cost not attributed |
| Health schema-cache miss calls initializer | Confirmed read-path admission gap | Current isolated DB: median 4.15 ms; 77 DDL + 2 DML per call. P2 measured cost; boundary correction warranted |
| No-expiry retention writes temporary JSONL copy | Confirmed unnecessary write | Isolated 2.02 MB input: same temporary bytes written, median 37.68 ms; daily default, P2 |
| Browser server request 5.647 s vs sequential median 122.4 ms | Confirmed observation; cause suspected | Queue/concurrency/shared-cache attribution needed before a fix |
| Watchdog full state / traffic reads; broad member import before probe budget | Suspected overlap | Different evidence contracts; no native cost attribution |
| API natural background CPU / RSS growth | Measurement needed | 16.3% one-core cgroup CPU over 150.7 s; RSS 461–485 MiB; no pressure/throttling or proven leak |
| Traffic full scan | Non-issue at current size | 328 rows, median 0.424 ms; do not add speculative indexes |
| Six inventory calls | Non-issue | Six distinct roles; single-flight shares cold full projection |
| Normal observed provider calls | Non-issue in bounded scope | Zero in observed GET/browser flow; no claims about every possible workflow |

The Direct admission rule is already correct: deliberate global Direct with no scoped VPN subjects exits before VPN recovery. Fixed/scoped VPN under global Direct still requires monitoring. Temporary Emergency Direct with desired VPN must still attempt verified re-entry. Preserve Core ownership, snapshot/probes-outside-guard, revision/incarnation fences, exact readback/CAS, last-good, provider-unknown-not-DOWN and disabled automatic-switch admission.

## Measurements and evidence

- [Source paths, SQL plans and isolated profiles](PATHS_DB.md): topology 3 SELECT / 3.03 ms median; provider projection 9 SELECT + 3 BEGIN / 5.47 ms; effective pool signature 4 SELECT / 3.85 ms. These are not HTTP query counts.
- [Live/UI/resource method and results](LIVE_UI_RESOURCES.md), [sanitized samples](LIVE_RESOURCE_SAMPLES.json): 150.7-second natural-workload observation plus a separate 30-second I/O window, bounded GETs and one EN/RU desktop browser flow. No meaningful p99 estimate or mobile performance claim.
- [Metadata-only native dataplane timing](DATAPLANE_READ.json): n3, median 201.72 ms, max 204.82 ms; does not time the entire Health path.
- [Synthetic retention results](RETENTION_SYNTHETIC.json), [isolated reproducer](scripts/retention_microbenchmark.py): n5, temporary files only, not physical SSD write attribution.

Current native generation/validation/reload time, complete request SQL count/serialization, transaction/lock contention, per-thread background attribution, durable daily storage/log growth and provider cache/gate counters remain unmeasured. Host SSD, process I/O and cgroup device counters do not reconcile; no wear forecast is justified.

## One implementation sequence

1. Separate read-only schema inspection from startup/installer initialization; preserve drift and migration/security semantics.
2. Attribute existing cold inventory/Health components, then scope confirmed role admission without a parallel Health model or lost runtime enforcement evidence.
3. Re-measure residual UI burst/server payload/render behavior; change only confirmed work after consumer review.
4. Attribute watchdog/member/background overlap and natural workload; change cadence/read scope only with evidence.
5. Address no-expiry retention writes with concurrency-safe before/after tests.
6. Consider DB connection/no-op receipt/index candidates only after actual cadence/write/lock measurements. Current sub-ms scans are not index work.

Every fix requires comparable before/after workload/cache evidence. No module extraction, UI redesign, new observability collector or speculative future-version infrastructure is part of these fixes. Stage 5 and the subsequent database architecture audit remain separate.

## Test and deployment boundaries

Production read-only measurement and isolated microbenchmarks are not L1–L7 acceptance suites. Only L0 audit-artifact syntax/JSON/documentation checks were required; no L6, no destructive L7, no browser/runtime installation. Subsequent implementation uses affected L0–L5 under the [Test Architecture contract](../../PROJECT_MAP/TEST_ARCHITECTURE_AND_CICD_FOUNDATION.md), L6 only by policy and L7 only disposable staging.

Source: audit artifacts only. Tests: bounded measurements + L0 artifact checks. Commit: this documentation/evidence checkpoint. Deploy: none. Live: bounded reads only, not failure/recovery or changed-input generation acceptance. Earlier live correctness evidence remains historical and is not current timing evidence.

## Artifact validation

L0: JSON parsing, isolated helper AST syntax, Markdown local evidence-link resolution and Git whitespace checks passed. No product suites were run. Consolidated local report SHA-256: `fed8e93dd85e91b64ca8f9bf517240cf92a3c957083655c0e0a0e095b6a27b71`.
