# Package3 implementation — source checkpoint

## Scope and state

PR15, branch `perf/operational-package2-3`, baseline source HEAD526221c after qualified Package2. Actual Package3 implementation, not closure-by-deferral. Independent combined QA PASSED:105 tests in10.49s; final full required CI is PENDING. Earlier fullCI38826004751 qualifies the previous HEAD only. No production changes, native refresh, credential operation, deployment or restart.

## C3: unchanged JSONL output

`event_contract.scrub_jsonl_files` previously wrote/fsynced a same-directory temporary output even when full sanitization found no change. The revised function always scans every record. Stable unchanged input returns without temp creation/output fsync. Changed input is rewound on the same opened source; a second sanitized pass produces atomic output. Source digest, inode/device, size, mtime/ctime and mode are checked to reject observed append/replacement/in-place/mode mutation, including during fsync. Failure preserves original/source replacement and removes owned temporary output; errors propagate.

Startup calls this after bootstrap and before handler registration/schedulers; the v21 migration also invokes it. This does not add an interprocess writer lock. An uncooperative writer can still race after final check and before replace, an inherited residual boundary. Do not advertise absolute concurrent-writer atomicity. No mtime-only security skip, retained whole-file buffer, sanitizer relaxation or missing-secret waiver.

First pre-edit RED and later expanded baseline replay are preserved separately in private `pkg3-scrub/FIRST_RED.*` and `red-final-*`. Tests cover full scan/no temp/no fsync, redaction/malformed/blank/CRLF/mode, replace/fsync failure cleanup, append/replacement and late fsync-time drift. Targeted16PASS; unchanged startup contract retained. Later replay is not represented as first-red chronology.

Matched five untraced repeats on Python3.11.2, frozen526221c baseline, synthetic-only network namespace. [Raw matrix](evidence/package3-scrub-matrix.json); instrumentation scoped to owned file-operation counters, not physical NAND.

|10k records /1.03MB|Baseline|Candidate|
|---|---:|---:|
|Unchanged wall median|283.788ms|243.313ms|
|Unchanged CPU median|276.619ms|243.989ms|
|Unchanged temp output|1.03MB /1fsync|0B /0fsync|
|Changed wall median|243.519ms|487.823ms|
|Changed CPU median|241.432ms|484.924ms|
|Changed input reads|1.03MB|2.06MB|
|Changed output/fsync|970KB /1|970KB /1|
|Separate traced unchanged peak|46,275B|23,198B|
|Separate traced changed peak|46,586B|48,115B|

Changed output digests/modes match. The changed-file CPU increase (~101%) is a measured tradeoff for the second validation pass, not hidden as a speedup. Canonical sorted log writers and startup-only placement make unchanged-file savings applicable; no production frequency/savings estimate is claimed.

## Operation-owned SQLite projections

Both `collect_xray_runtime_bindings` and `collect_xray_client_mode_directives` selected IDs, then loaded each subject, its metadata detail and both override types through separate DB sessions. They now select full eligible rows and reuse existing detail/override batch helpers in bounded500-ID chunks. Prefix exclusion happens before enrichment/override loading. Existing policies, order, source filtering, server config/handoff reads and per-subject emergency checks remain. No change to Core/native apply, durable identities, CAS/fencing, migrations, connection initialization or credentials.

Each invocation reads anew; there is no persistent cache. Subject rows are captured at initial selection, with short subsequent detail/override sessions. This is a request-scoped projection, not an atomic snapshot of all independent tables or fresh native proof. Native/writer owners retain final validation. Tests cover current/expired/null overrides, legacy metadata fallback, deletion/change between calls, independent cohorts and1005 IDs without truncation. Original mock fixtures are expanded to full selected rows; existing inclusion/exclusion assertions remain. Baseline RED and chunk-bound RED preceded the corresponding source corrections. Targeted5PASS.

[Both-collector raw pairs](evidence/package3-projection-pairs.json), [small workloads](evidence/package3-projection-small.jsonl), [separate allocations](evidence/package3-projection-allocations.json). Five interleaved untraced pairs; synthetic500 subjects/50 overrides. Routing/native observations supplied controlled fixtures; no real native endpoint or production data. Output equality normalizes only volatile `applied_at`.

|Both collectors /500 subjects|Baseline|Candidate|
|---|---:|---:|
|Wall median|4513.2ms|64.1ms|
|DB sessions /SELECTs|4002 /4002|6 /6|
|Emergency checks|1000|1000|
|Separate traced peak|755,954B|2,242,242B|

This is ~98.6% lower component wall time, with ~1.49MB extra transient peak from batching; it is not production RSS/CPU or SSD/fsync saving. Session count scales with bounded chunks and applicable detail tables, not always a constant6. Small0/1/10-row paired medians:2.42→2.40ms,13.42→8.18ms,109.2→10.7ms. No new expensive-state caching or optional-module requirement.

## Verification and remaining gates

Both Writer candidates frozen; independent QA PASSED with105 tests in10.49s and matching four source/test hashes. Full combined-source regression and exact finalHEAD required CI must pass before Ready. Package2 hash-bound source remains unchanged. Preserve canonical/newer PR14 documents and pending patches. User alone merges; actual main CI then separately authorized rollout/live acceptance. Stage5 and unrelated optimizations are not started.

## Current-main repeated-bootstrap defect — corrective continuation

User-merged PR14 main2d53daf introduced no runtime changes, but mainCI38829503900 exposed an existing schema10→current repeated-bootstrap idempotency failure. Isolated exact-main reproduction passed within one second and failed with1.1s separation. Only timestamps changed: `subjects.updated_at` for fwrouter:global/host:ssh and corresponding detail rows. `ensure_builtin_system_subjects` and its detail UPSERT unconditionally rewrote canonical unchanged data. Conditional null-safe owned-field UPDATE/UPSERT repair is implemented with deterministic old-timestamp regressions; existing migration assertion stays unchanged. No SQLite setting/schema/native contract changes or physicalSSD savings are inferred. Writer affected55PASS (bootstrap/db_migrations/system_subjects/taxonomy); independent QA PASSED71 tests in8.63s (bootstrap/system_subjects/scoped_egress/db_migrations). The cohorts differ; final exact-HEAD CI remains pending. The regression proves repeat ensure produces SQLite total_changes delta0 after mandatory global override cleanup, while real/null/pending/revival changes still repair and update timestamps. No sleep or frozen clock masks the original condition.
