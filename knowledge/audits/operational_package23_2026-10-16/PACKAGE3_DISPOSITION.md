# Package3 resource I/O disposition

## Current implementation and remaining dispositions — 2026-10-16

Package3 is implemented locally in the existing PR15, independent combined QA PASSED (105 tests,10.49s), pending final exact-HEAD full CI. The user corrected a short-lived closure-by-DEFER decision; that closure is superseded, not implementation evidence. Production remains unchanged. The earlier chmod pilot below is preserved as historical evidence.

- **C3 IMPLEMENTED:** full security scan remains; unchanged JSONL creates no temporary output and performs no output fsync. Changed files use a second pass, source digest/signature checks, preserved mode, fsync and atomic replacement. Detected concurrent mutation aborts safely. Startup precedes application schedulers; no absolute exclusion of arbitrary external writers is claimed.
- **Operation-owned subject/override projections IMPLEMENTED:** both existing Xray binding collectors reuse existing batch helpers, in chunks of at most500 IDs. Exact selectors/order/exclusion remain, each invocation rereads, no global or cross-call cache. Native readback, emergency checks, credentials and publication/fencing owners remain untouched. This is an optional module optimization, not a Core dependency on Xray.
- **C4 conditional chmod DEFER:** prior overlapping marginal pilot remains rejected; no SQLite connection-factory change.
- **PRAGMA/WAL/synchronous/transactions/fsync guarantees NO CHANGE.** No durable state buffering or weaker durability.
- **Additional native readback/projection reuse and SSD wear claims DEFER:** these are not prerequisites for accepting the two implemented improvements and are not represented as implemented.

Source and measurements: [implementation report](PACKAGE3_IMPLEMENTATION.md). Closure requires the final combined-source qualification; merge/main CI/deploy/live remain separate gates.

Historical pilot checkpoint, 2026-10-16. Scope: deep-audit candidates C3/C4 before the implementation continuation. The source tree was restored to branch HEAD (`3f96e1e73be0e0fc25208a406ecc9ff259f6480f`) for owned paths. No tests, commits, pushes, CI, production operations, database changes, or event scrub changes were performed. `event_contract.py` was not edited. Pilot code and its temporary tests were removed after measurement; scripts and receipts remain in this directory.

## Matched C4 measurement

The workload used the actual `connect()` implementation with an overridden DB path pointing only under `/root/fwrouter-recovery/package23-20261016/io/`. Each session opened a connection, issued one `SELECT`, and closed it. The DB was initialized before measurement. Five repeats used 500 sequential sessions each, with separate baseline/candidate synthetic state roots. The baseline module came from `git show 3f96e1e:backend/fwrouter_api/db/connection.py`; the candidate was the temporary mode-aware conditional `stat`/`chmod` helper. Runs used `env -i`, source `PYTHONPATH`, the existing backend virtualenv, and no production inputs or external credentials. No network calls are in the measured code. This was a host-local synthetic microbenchmark, not an isolated VM or production workload.

| Variant | Wall median (range) | CPU median (range) | Per 500 sessions: chmod | Per 500 sessions: stat |
|---|---:|---:|---:|---:|
| Baseline | 103.99 ms (101.92–107.03 ms) | 103.96 ms (101.91–107.03 ms) | 2,500 | 2,000 |
| Conditional repair pilot | 101.45 ms (100.17–102.50 ms) | 101.43 ms (100.11–102.45 ms) | 0 | 4,500 |

The approximately 2.4% median difference favors the pilot, while ranges overlap and only one shared host was measured. A smaller 100-session preliminary pair showed the candidate slower by about 13%, consistent with measurement noise at that scale. Separate 20-session tracemalloc peaks were 4,298 B baseline and 4,302 B candidate. Parent/database modes remained 0700/0600 in both variants. This is not enough evidence of a practical production win: the pilot replaces each same-mode chmod with a stat, there is no observed production call rate, and kernel/SSD metadata-write savings were not measured. **Decision: C4 DEFER; no source change.**

The preliminary pair is retained in `baseline.json` and `candidate.json`; five-run distributions are in `baseline-repeated.json` and `candidate-repeated.json`. `baseline_connection.py` is the frozen source copy and `bench_runner.py` is the matched harness. `baseline.py` records the first pair harness.

## Historical pre-implementation candidate dispositions

- **C3 — startup JSONL scrub temp output: DEFER.** The deep audit measured 10,000 canonical synthetic records / 1.03 MB and one fsync per unchanged-file pass. Avoiding unchanged temp output still requires the complete security scan. A bounded no-temp strategy that preserves the current behavior under concurrent append/replacement, malformed text, file mode drift, late changes, write failures, and atomic replacement has no established snapshot contract in this scope. No `event_contract.py` edits.
- **C4 — conditional DB/parent/WAL/SHM permission repair: DEFER.** The pilot reduced same-mode chmod calls but added the same number of stat calls and showed only a small, overlapping synthetic time difference. Wrong-mode, concurrent-create, and sidecar tests were drafted but not retained because there is no shipped implementation and no test run.
- **SQLite PRAGMAs, WAL, synchronous mode, transaction boundaries, commit/fsync behavior: NO CHANGE.** No durability or transaction-sharing proposal is supported by this task's evidence.
- **Repeated native/projection work: DEFER.** No owned-path per-caller invocation cost, freshness impact, or proof-preservation evidence was produced here.
- **SSD write reduction: DEFER.** Existing selected counters do not establish physical NAND writes or C4 fsync savings; production owner attribution remains unknown.
- **Configuration regeneration: NO NEW QUALIFYING CASE** in the owned paths.

## Limits

This evidence is limited to a sequential synthetic SQLite setup workload and a small number of local repetitions. It does not estimate production request/connect frequency, filesystem metadata persistence, kernel writeback, physical SSD/NAND wear, concurrent service contention, or confidence intervals. No regression test was run; the source change was intentionally removed after the evidence review.
