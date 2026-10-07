# Stage 4B measured completion package

Source baseline: `49591c2`. Date: 2026-10-07. This report separates source delivery from deployment and final live acceptance; see [test receipt](SOURCE_TEST_RECEIPT.md) and the subsequent live receipt for exact gate outcomes.

## Scope and architectural decisions

Deploy and accept the existing scoped-bindings admission fix first, then address measured runtime read overhead, maintenance admission, and duplicate candidate validation. No Stage 5 work, provider mutation, forced outage, routing redesign, persistent cache, speculative index, retention format or scheduler-frequency change is included.

1. **Scoped bindings:** `49591c2` was installed using the standard backend/docs installer and an API-only explicit restart. Binding reads stay behind existing path/active/target eligibility gates. The prior matched synthetic fixture proved 524→8 reads/parses with identical output; that is not a production page-load acceleration claim.
2. **Maintenance admission:** schema-check uses existing read-only schema inspection. Cleanup requires an existing compatible schema before its handler; it does not run API bootstrap, migrations, zero-age stale-job cleanup, DNS reconciliation or startup runtime recovery. Rebuild retains its own source-resolution and selection-fence preflight. Missing/drifted schema fails closed with a nonzero CLI exit. Real maintenance retains its intentional expiry, probes and retention operations; API startup remains unchanged.
3. **Runtime counters:** one terse JSON listing of the owned nft table replaces five native chain subprocesses. Counter/comment ordering and missing-data projection remain equivalent; unavailable/malformed table or affected chains use the previous line-parser fallback. Existing cache TTL, evidence semantics and readback ownership are unchanged.
4. **Generation validation:** a successful native validation is reused only for an identical candidate SHA and immutable local image ID within the same generation. Validators run against that exact ID; failure to resolve it disables reuse. Local structural validation, candidate-integrity checks, each distinct candidate's native validation and final runtime/native verification remain mandatory. No receipt is inferred from unchanged intent or an active-file hash alone. The first validation is required evidence; skipping it across generations would weaken the existing safety contract.

Core sole ownership, selection revision/incarnation/CAS, desired/effective Emergency Direct, exclusive-source eligibility, provider-member intent and fixed Xray bindings are untouched.

## Attribution and before/after

Detailed evidence: [summary/native](SUMMARY_AND_NATIVE_EVIDENCE.md), [counter aggregation](COUNTER_AGGREGATION.md), [I/O and retention](IO_REPORT.md).

| Path | Before | Correction / after | Boundary |
|---|---|---|---|
| Scoped explicit-client projection | 524 binding reads/parses in matched synthetic fixture | 8; identical output | Microprofile; already deployed as `49591c2` |
| Maintenance command admission | Full API bootstrap before command branching | Zero bootstrap calls; read-only schema gate or rebuild-owned preflight | Isolated negative-call/command tests; no live cleanup forced |
| Counter native calls | Five listings; median 289.2 ms, n3 | One terse table listing, median 68.1 ms; complete projection median 80.1 ms, n3 | Table output 98,130 B vs 6,448 B; exact-value parity uses deterministic fixtures |
| Identical transition/final native validation | Two native checks, each about 720 ms median, n3 | One check plus local image-ID resolution | Same-operation reuse only; measured native time includes Docker startup |
| Cold summary attribution | Direct-route observer 1,534.5 ms | 1,351.9 ms; nft subprocesses 9→5; counter node 285.7→74.1 ms | n1 per profile, not HTTP p50/p95; 76 SQL statements and 7,403-byte payload retained |

The summary observer uses a private SQLite online-backup copy and reads the actual native/file contour. SQLite represented roughly 6% of one observed handler duration; serialization was below 1 ms. API queue/scheduling was not independently instrumented for that same request. Process CPU deltas include background activity. Neither subtraction from mocked timings nor summing nested/concurrent nodes is used to attribute latency.

See [before/after receipt](SYSTEM_SUMMARY_BEFORE_AFTER.json). This establishes the eliminated four native subprocesses and counter projection gain; remaining roughly 1.35 s is not declared free of all potential optimization. No further redundant work was proved in that residual required-observation contour. The historical burst/queue cause is not retrospectively assigned from these serial measurements.

## Expected work and monitoring tails

The configured API maintenance scheduler is disabled; daily systemd maintenance is not duplicated by an API maintenance loop. The separate daily retention dry-run is a documented canary with intentional job lifecycle writes. Real cleanup runs had expired rows and actual deletion/VACUUM work. The already-delivered JSONL no-expiry rewrite gate remains present. No new no-op WAL/write defect was proved.

Long-window WAL/device-write amplification, retention bytes rewritten, SSD/storage growth and background I/O remain monitoring tails. A short idle stat window is not an SSD-write measurement. Historical fallback alternations have a known native selection mechanism, but exact old health triggers/overlap remain unproved; this package does not recreate old groups or promote that historical uncertainty into a current regression.

## Tests, release and acceptance

Affected L0–L5 only; no L6/L7. Exact tests, known failures reproduced on `49591c2`, native provisioning boundaries and manual selected-plan coverage are recorded in the test receipt. Baseline failures remain visible; no blanket exception or CI-green claim is added.

Source changes were architecturally reviewed before commit. Final backend/docs deployment and live acceptance are recorded separately in `LIVE.md`; until that receipt passes, Stage 4B closure is pending. Rollback is code-only through the standard installer to the reviewed parent, retaining the current database and persistent intent. Protected SQLite/config/deployed-source backups are local and are not committed.
