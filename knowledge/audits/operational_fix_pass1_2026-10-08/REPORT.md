# Operational Performance Fix Pass 1

Date: 2026-10-08. Reviewed source baseline: `4bb2391`; deployed application before this package: `7de9f88`. No schema, routing, provider workflow, runtime lifecycle, scheduler, index, new cache, or parallel execution changes.

## Confirmed scope

1. Remove five redundant outer JobManager cleanup sites immediately preceding service APIs that already run configured stale queued/running cleanup. Service conflict checks/uniqueness and explicit worker cleanup remain.
2. Identical canonical Settings saves skip DML/commit/cache invalidation/prewarm. Changed, absent, malformed, legacy and type-different values retain the durable write and invalidation path.
3. System summary reuses enforcement from its existing runtime summary snapshot instead of building it twice. The opt-in internal projection validates shape/bypass consistency, preserves the public DTO, and falls back to the authoritative builder if the snapshot is unusable. Existing freshness policy is unchanged.
4. Operational event writer constructs its return projection from exactly committed producer-owned columns, eliminating the post-INSERT SELECT. Commit occurs before JSONL append. No batching: 100 independent events still have 100 durable commits.

## Measurements and boundaries

| Isolated cohort | Before | After | Confirmed removed work |
|---|---:|---:|---|
| 100 unlocked job creates, wall / CPU | 568.03 / 376.26 ms | 471.42 / 276.87 ms | cleanup 200→100; SELECT/connections 300→200 |
| 100 identical Settings saves, wall / CPU | 206.86 / 206.77 ms | 202.80 / 202.65 ms | 100 DML/commits/prewarm dispatches→0 |
| Uncached summary, n9 median wall / CPU | 48.42 / 38.94 ms | 44.15 / 34.74 ms | enforcement 2→1; SELECT25→22; connections24→21 |
| 100 operational events | see paired evidence | see paired evidence | writer SELECT100→0; commits/output unchanged |

Job and Settings results are single cohorts, not production quantiles. Settings latency difference is too small for a useful acceleration claim. Logging timing ranges are noisy; deterministic call counts are the acceptance basis. Current RSS/process HWM are distinguished; no reliable operation peak-RSS reduction is claimed. Process-accounted write_bytes is not SSD/fsync attribution. Settings/logging write_bytes did not fall; no write-amplification improvement is claimed. Read [background evidence](BACKGROUND.md) and [summary/logging evidence](SUMMARY_LOGGING.md) for reproducible inputs, bounds, and exact results.

## Preserved work / Pass 2 candidates

Xray create/delete adapter mutation and subsequent canonical binding materialization produce different candidates. Retain both validation/reload/readback stages; exact-text materialization no-op already exists. Alias edit remains metadata-only. [Lifecycle attribution](XRAY.md). A consolidated lifecycle apply needs separate design and candidate/readback evidence.

Bootstrap schema/migration/orphan recovery/taxonomy/builtin repair remains. Changed Settings prewarm remains. Arbitrary event batching would weaken synchronous per-event durability and is not implemented. Independent runtime/summary steps remain sequential; bounded parallelization is a Pass 2 candidate only after ownership/resource review. No speculative cache/index or fixed-binding changes.

## Test acceptance

Affected component suites:135 passed (28.57s), JobManager/Settings/runtime summary/events/core bypass/scoped egress. Additional L5 anchors cover Core selector interleavings, provider recovery, exclusive source, subscription outcomes, Xray generation recovery and Xray VPN-auto lifecycle. L0 syntax/import/manifest/surface checks and isolated L4 smoke are required. No L6/L7/native repeat for unchanged candidates.

The L5 cohort reports 154 passed and one failure: `test_xray_vpn_auto_lifecycle.py::test_vpn_auto_reconcile_removes_stale_identity_and_recreates_one_stable_identity`, KeyError `deleted_count`. That exact failure reproduces on immutable `4bb2391` (0.30s). It is baseline-existing; not fixed/allowlisted by this package. No full-suite green claim. Direct runs remain distinct from CI receipts; immutable affected-gate execution cannot cover uncommitted source.

## Delivery

Source/test commit and standard backend/docs deployment, protected backup, runtime exact readback, native-receipt reuse by exact config digest, quantitative same-process provider bracket, and observation are separate gates. [Bounded live acceptance](LIVE.md) completed for source `f78857f`; formal complete live-smoke tooling gate remains open. No provider PATCH/member switch/forced outage/client mutation or subscription refresh is used for acceptance. Code-only rollback to `4bb2391` via installer/API restart; do not restore old SQLite over current state.
