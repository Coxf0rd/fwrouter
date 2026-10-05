# Stage 4B — evidence-based performance fixes

Date: 2026-10-05. Source baseline: `24ef1ba`. This checkpoint implements a bounded first batch of Stage 4B, not a blanket closure of all performance work. The preceding [Stage 4 audit](../performance_resource_2026-10-05/REPORT.md) and [UI supplement](../performance_resource_2026-10-05/UI_PERFORMANCE.md) remain historical before evidence.

## Ownership and scope

Persistent routing intent, Core selection/revision/CAS/provenance, exclusive eligibility, provider member policy, fixed Xray bindings, native configuration generation and runtime apply are outside the changed source. No new cache, index, polling mechanism, frontend optimization, layout or Auto-sorting behavior is introduced.

Three measured fixes:

1. Inspect the existing schema on Health reads; initialize and migrate only on the explicit startup/installer path.
2. Select the newest bounded technical-log candidates before recursively sanitizing their payloads; return the same filtered, ordered and redacted contract.
3. Avoid opening/writing a temporary JSONL rewrite when no log records expire.

## Comparable before/after

| Isolated path | Before | After | Evidence |
|---|---:|---:|---|
| Technical logs, newest 20, same private 12,595,532-byte / 15-file snapshot, n=3 | median 4,377.81 ms wall / 4,377.56 ms CPU | median 173.04 ms wall / 173.00 ms CPU | [aggregate result](TECHNICAL_LOGS_BEFORE_AFTER.json); exact returned payload equality |
| Full-payload sanitization per technical-log query | 1,066 records | 20 records | Same scan/filter set; no cache or skipped input files |
| Technical-log per-call Python allocation peak | 35,377,622 bytes | 1,542,167 bytes | Separate tracemalloc pass; not process RSS or leak evidence |
| Current-schema inspection, same 507,904-byte input, n=5 | median 3.052 ms; 77 DDL / 2 DML | median 1.827 ms; 0 DDL / 0 DML | [paired measurement](READ_RETENTION.md); explicit bootstrap unchanged |
| No-expiry JSONL cleanup, same synthetic 2.02 MB input, n=5 | median 8.55 ms; 2,020,000 temporary bytes/call | median 5.57 ms; 0 temporary bytes/call | [paired measurement](READ_RETENTION.md); not physical SSD attribution |

The technical-log improvement is approximately 25 times on this input. CPU and
allocation reductions directly match the removed full-history sanitization;
this is a helper measurement, not an end-to-end browser p95 claim. Runtime and
payload contracts remain unchanged. Browser/live evidence is a separate gate.

## Attribution and remaining work

Detailed nested timings, source/deployed file matches and the `/servers`
service-versus-browser boundary are in [ATTRIBUTION.md](ATTRIBUTION.md).

Pinned-source isolated profiling uses a private online SQLite backup and aggregate timing only; it does not run app startup or schedulers. Cold narrow inventory takes approximately 2.35–2.61 seconds, with most time in canonical Health/runtime enforcement. Recorded SELECT fetch intervals total only a few milliseconds; the original cursor wrapper starts after execute, so these figures exclude initial SQL execution and are not a complete SQL attribution. One narrow decomposition attributes 759 ms to dataplane-check, approximately 520 ms to nine read-only nft subprocesses, and 563 ms to global preflight including 274 ms of Mihomo health. These are nested timings, not additive independent categories. Local controller requests were GET metadata. Two existing preflight DNS subprocesses were observed unintentionally; later profiling stops and fails closed on unapproved calls. No provider request or runtime mutation was made.

Workspace takes approximately 4.48–4.65 seconds; technical-log listing alone accounts for 4.29–4.48 seconds while reading 15 JSONL files totaling approximately 12.6 MB. Recorded SQL fetch intervals are approximately 1.36 ms (initial execute excluded) and JSON serialization approximately 0.8 ms. This proves a backend log-processing cause for workspace latency. It does not prove that every `/servers` burst or browser pre-request delay has the same cause.

Global Health freshness/readback semantics remain unchanged. Moving role filtering alone would not remove the dominant global enforcement observation. A future scoped-admission or native-read batching change requires its own contract and comparable measurements. No speculative index or cache is justified by the measured SQL cost.

Remaining gates: native Health cold cost; exact `/servers` burst queue/GIL/background attribution; browser pre-request scheduling; background/member/watchdog CPU ownership; long-window SSD/WAL/log growth; remote/mobile performance. Large payloads and role-distinct requests are not automatically defects. Stage 5 follows accepted Stage 4B closure, not this partial batch.

## Tests and measurements

See [schema/retention evidence](READ_RETENTION.md) for paired same-input measurements and exact test classification. New focused tests pass. Selected L1–L3 and L5 anchors include eight failures reproduced by exact ID on isolated clean `24ef1ba`; they are reproduced pre-existing failures, not a new green full suite or a blanket CI exception. State API watchdog-row initialization ownership and the historical runtime assertions remain separately unresolved; four Home Assistant message expectations are stale assertions. Baseline reproduction proves age, not that every failure is benign. L0 checks and isolated L4 smoke are recorded separately. L6 and destructive L7 are not required or run.

Source, Tests, Commit, Deploy and Live are separate gates. The reviewed source/test checkpoint is `9a4dc03`. Delivery evidence and final comparable live measurements are added only after completion.

## Rollback contract

Deployment uses the standard backend/docs installer and only the API service needs an explicit restart. Preserve current SQLite intent, runtime artifacts, credentials and fixed bindings. If a critical invariant fails, revert code with the standard installer to the protected previous source and restart API; never restore an old database or blindly replay runtime selection. Existing API/gateway lifecycle coupling is observed, not redesigned here.


## 2026-10-05 — first batch delivery acceptance

Source commit `9a4dc03` was deployed through the standard backend/docs installer; only `fwrouter-api.service` was explicitly restarted. [Live evidence](LIVE.md) and [structured evidence](LIVE.json) confirm healthy API/schema 24, selection revision 37, unchanged Provider member 1456, exclusive intent/provenance, one eligible Auto target, native configs and runtime incarnations. All 78 Xray bindings and 10 handoff listeners retain semantic parity; only generated receipt timestamps changed. Related API/gateway/Mihomo/Xray units report success. No provider mutation or selection/apply workflow was initiated by this batch; a quantified postdeploy provider-request counter is not recorded in this acceptance artifact and is not inferred from HTTP success.

One browser flow before/after (not a matched cold/warm distribution): workspace TTFB 4,835.3 → 601.9 ms; Admin phase 5.270 → 1.047 s; User 3.500 → 4.551 s; Settings 0.684 → 1.117 s. User/Admin `/servers` TTFB did not improve (137.6 → 158.3 / 287.4 → 310.6 ms). No page-level JS exceptions or visible alert errors occurred; the intentional external-IP abort generated a console error in both runs. Broad UI acceleration and elimination of the historical 5.65-second burst remain unconfirmed.

Protected predeploy backup: `/var/lib/fwrouter-v2/backups/stage4b-read-retention-9a4dc036-predeploy-20261005`. Rollback is code-only via the standard installer to `24ef1ba`, retaining the current database and native runtime; no old DB restore. First batch Source/Tests/Commit/Deploy/bounded Live gates are complete with the recorded baseline failures and measurement limits. Stage 4B remains active; residual cold Health/runtime cost, burst/scheduling/background attribution and long-window resource measurements remain open. Stage 5 is not started.
