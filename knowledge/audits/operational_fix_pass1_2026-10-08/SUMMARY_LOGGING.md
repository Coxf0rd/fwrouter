# Operational Performance Fix Pass 1 — summary and operational logging

Baseline: source `4bb23916fbe346d59e42f91eb6883903e7b59316` (`4bb2391`). Measurements below use a fresh temporary SQLite/config root per process, the same existing virtualenv, fixture inputs, SQL tracing, process CPU/I/O counters, and deterministic runtime boundaries. Native commands, sockets, providers, and runtime mutations were denied or replaced by fixed test fixtures. These measurements are isolated control-plane evidence, not production HTTP or physical storage results.

## Confirmed fixes

`/api/v2/system/summary` previously built runtime enforcement once as part of the cached runtime summary used for scoped-egress state, then called `build_runtime_enforcement_state()` again. The system summary now opts into the enforcement fields already present in that same runtime snapshot. Extraction excludes the nine summary-only dataplane metadata keys and validates the expected enforcement shape. It retains the optional `bypass` field only when the canonical core-bypass and enforcement projections both identify active bypass. A malformed or inconsistent snapshot falls back to the prior authoritative builder. The default scoped-egress projection keeps its original four-key shape; the internal enforcement value is removed before the public system summary is returned.

`write_operational_log` previously selected the inserted row by event ID before returning. It now constructs the same `_row_to_event` input from the exact values inserted into the current trigger-free schema, only after the DB session commits. Each event still gets an independent synchronous SQLite transaction; JSONL append remains after commit, preserving per-event durability and ordering. Redaction, message bounds, timestamps, and returned shape still pass through the existing canonicalization and row projection helpers.

## Before/after

### System summary

Three fresh processes per side, each making three uncached calls (nine samples per side), using the baseline worktree and current source with the same isolated fixture:

| Measure | Baseline | After |
|---|---:|---:|
| Wall time, median (range) | 48.42 ms (42.89–53.58) | 44.15 ms (39.57–51.28) |
| Process CPU, median (range) | 38.94 ms (35.12–43.63) | 34.74 ms (32.00–42.12) |
| SQL reads | 25 SELECT / 24 connections | 22 SELECT / 21 connections |
| SQL writes / commits | 0 / 0 | 0 / 0 |
| Enforcement builder calls | 2 | 1 |
| Live dataplane payload reads | 1 | 1 |
| Process-accounted `write_bytes`, median | 782,336 B | 684,032 B |
| Process HWM delta observed | 16–1,356 KiB | 0–3,188 KiB |
| Temporary SQLite / WAL after close | 507,904 B / 0 B | 507,904 B / 0 B |

The observed `runtime_enforcement` and scoped-egress readiness projections were equal across baseline and after runs. Full serialized response hashes differed because each run uses a different temporary root in the `paths` fields. The HWM deltas are short-process observations after imports and varied between runs; they do not establish a summary-specific RSS reduction. No external process/native validation ran in this fixture.

### 100 operational events

Three fresh isolated processes per side. The surrounding host was not idle; the ranges overlap, so latency and CPU changes are descriptive for this small cohort. SQL action counts are deterministic.

| Measure | Baseline | After |
|---|---:|---:|
| Wall time, median (range) | 429.7 ms (421.0–462.5) | 450.1 ms (428.3–505.8) |
| Process CPU, median (range) | 230.6 ms (227.5–251.1) | 226.7 ms (225.5–228.1) |
| Event writer SELECTs | 100 | 0 |
| Event writer INSERT / BEGIN / COMMIT | 100 / 100 / 100 | 100 / 100 / 100 |
| Event writer DB connections | 100 | 100 |
| JSONL rows / bytes | 100 / 66,490 B | 100 / 66,490 B |
| SQLite file / WAL after close | 557,056 B / 0 B | 557,056 B / 0 B |
| Process-accounted `write_bytes` | 6,803,456 B | 6,803,456 B |
| Process HWM delta, median | 720 KiB | 752 KiB |

The benchmark performs one separate `list_operational_logs` readback after the measured writer loop; it records one SELECT there on both sides. The old writer itself performed one SELECT for every event; the new writer performs none. Process-accounted I/O, JSONL size, database size, and commit count were unchanged. These counters do not measure physical SSD writes or fsync latency. No native/runtime/provider actions occur in the logging path.

The paired log samples do not establish a wall-latency improvement: median wall time was slightly higher after the change while CPU medians were close and ranges overlap. The confirmed improvement is removal of 100 redundant readbacks while preserving 100 commits and identical output size, not a stable latency target.

## Semantics and tests

The tests cover the default scoped-egress projection shape, enforcement extraction for applied direct/selective/VPN modes, active bypass handling, missing-runtime fallback, and malformed bypass consistency fallback. System summary tests assert only one enforcement build and the same projected enforcement result. Logging tests compare the returned event with SQLite readback, retain timestamps/details/redaction, assert zero SELECTs in the writer, preserve primary-key conflict behavior, and verify that an injected commit failure produces no JSONL append.

Validation:

- Python syntax compilation and `git diff --check`: PASS.
- Focused existing L1/L2 suites (`test_runtime_summary.py`, `test_events_api.py`, `test_core_bypass.py`, `test_scoped_egress.py`): 66 passed.
- No native Xray/Mihomo validation, L6, or L7 was run.
- The exact changed-path gate plan was generated (L0–L3 plus L5 runtime anchors), but `gate run` rejected it because the required immutable `base..HEAD` diff is empty while source changes remain uncommitted. The direct suite above is the completed test evidence; this does not claim the unrun anchors passed.
- Reproducer: `summary_logging_probe.py`; paired runner: `run_summary_logging_probe.py`. Raw output from three paired processes per side for both modes is in `summary_logging_raw.json`. Each summary process contains three measured calls. The summary response hash includes temporary root paths and therefore varies by process; the enforcement and scoped-egress projections are the comparison targets.

Source changes are not committed or deployed in this evidence package. Production `/system/summary` timing, live parity/readback, provider brackets, and observation-window acceptance remain root-owned release gates.
