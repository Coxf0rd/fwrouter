# Stage 4B attribution

Date: 2026-10-05. Source baseline: `24ef1ba12c8707dc7a40dccf8bf32491a36319e7`. This is an attribution checkpoint plus one approved technical-log read optimization. It does not close the remaining Stage 4B cold Health/runtime or `/servers` burst gates.

## Method and boundary

The first source profiles used `git archive 24ef1ba` and a private SQLite snapshot created with the database opened through `mode=ro`. The narrowed profile used the same pinned source archive and snapshot. It imported service functions only: no FastAPI app, server startup, or scheduled runtime was imported. The measurement harness recorded SQL statement classes and fetch intervals, named service-helper wall time, JSON encoding time/size, controller request category and elapsed time, and subprocess basename and elapsed time. It did not retain raw SQL, arguments, controller URLs, or response bodies in the evidence. API payloads were serialized only in memory for byte and timing counts.

The deployed `/opt/fwrouter-api` SHA-256 matched the pinned source for the route and service files used here: `routes/ui.py`, `routes/servers.py`, `services/ui_state_inventory.py`, `services/ui_state_common.py`, `services/state_projection.py`, `services/ui_state_summary.py`, `services/custom_servers.py`, `services/server_inventory.py`, `services/logs.py`, and `services/provider_managed.py`. This establishes those file matches, not a complete release-manifest match.

The captured SQLite wrapper started its timer after `sqlite3.execute` returned, so the quoted per-query totals cover fetch work only and exclude execute time. The durable profiler script now starts the timer before execute for a future explicitly authorized run; the cold profile was not repeated.

The read-only Health helper unexpectedly launched `dig` twice during its existing preflight path (about 21 ms each). A `dig` invocation may make a DNS query and exceeded the intended controller-metadata-only boundary. I stopped runtime profiling after finding it; this report does not claim zero probes or zero network lookups. The narrowed run captured only loopback HTTP GETs to Mihomo metadata routes (`/version`, `/proxies` family, `/connections`); it captured no provider host or non-GET request. The older broad run was bounded but repeated role and workspace paths three times; the narrower results below supersede it for the per-call attribution.

## Narrowed cold/warm service profile

The profile used one cold and one immediate warm inventory and workspace call, and one cold/warm active server inventory call. Runtime metadata helpers followed the ordinary read path. The harness did not call provider refresh, manual checks, apply, or state-changing API requests; the indirect `dig` subprocesses observed under Health are documented above.

| Path / stage | Cold | Warm / detail | Attribution |
|---|---:|---:|---|
| Settings inventory, `router_core`, 1 returned row | 2,391.5 ms | 5.0 ms | All-subject canonical Health projection: 2,382.5 ms; runtime enforcement: 1,903.0 ms; 207 SELECT fetch intervals totaled 3.76 ms, excluding execute time; JSON encoding 0.10 ms / 1,203 B. |
| Health runtime enforcement | 1,903.0 ms | — | `dataplane-check.sh`: 759.1 ms; live payload helper: 1,136.8 ms; global preflight: 562.5 ms, including Mihomo health at 273.9 ms; live global-mode nft read: 56.0 ms. Timings are nested and must not be summed as exclusive stages. |
| Runtime metadata beneath Health | — | — | Nine Python-level `nft` subprocess calls (median 57.2 ms, max 89.1 ms); the shell check also performs its own read-only table/route inspection. Loopback GETs: `/version` n=1, `/proxies` family n=5, `/connections` n=1; those HTTP requests were only a small part of adapter wall time. |
| Settings workspace | 4,636.6 ms | 0.003 ms via its 2 s cache | `list_technical_logs(limit=20)`: 4,450.2 ms to read and sanitize 15 files / 12,595,532 B before sorting and truncating. Other measured helpers were at most 34.1 ms; 34 SELECT fetch intervals totaled 1.35 ms, excluding execute time; JSON encoding was 0.82 ms / 58,956 B. |
| `/servers?inventory_state=active&limit=1000` service path | 97.6 ms | 104.5 ms | 30 rows / 208,682 B; 50 SELECT fetch intervals totaled 1.63 ms, excluding execute time; JSON encoding 2.52 ms. Runtime topology helper took 60.7–66.9 ms. This was an isolated service call, not HTTP thread-pool queue time. |

The cold inventory call spends 1,903 ms inside the canonical global runtime-enforcement helper, while the returned-row serializer takes 0.10 ms and the seven counted Mihomo HTTP GETs are individually short. SQL execute time was not separately captured, so the fetch figures alone cannot rule out database execution cost. Filtering Health subjects by role would still pay the measured global runtime helper cost and could affect freshness semantics, so it is not an approved seconds-level fix. Any narrower Health admission change remains a separately measured and contract-reviewed gate.

The isolated active `/servers` service call is around 0.1 s, consistent in scale with the earlier sequential HTTP p50 of 122.4 ms. The earlier concurrent browser `/servers` observation was 5.647 s. No thread-pool queue timer or matched concurrency attribution was collected here, so queue delay, background overlap, and any GIL contribution remain unproven. No concurrent load was generated for this checkpoint.

## Approved technical-log optimization and before/after

`list_technical_logs` must parse and filter every readable JSONL line to preserve existing file/error behavior, but it now holds only the newest bounded candidate set and calls the recursive sanitizer only for returned records. The heap key retains timestamp ordering and original file/line order for equal timestamps. Existing component filename normalization, level/event filters, malformed-JSON file boundary, JSON non-object skipping, and returned-record sanitization are covered by six isolated tests in `backend/tests/test_technical_logs_selection.py`.

Before/after used the exact same private copy of the technical log tree (15 files, 12,595,532 B), three listing calls per implementation, and `limit=20`. The baseline implementation came from `24ef1ba`; the candidate came from the working tree. Exact returned payload equality passed. See [TECHNICAL_LOGS_BEFORE_AFTER.json](TECHNICAL_LOGS_BEFORE_AFTER.json) and [compare_technical_logs.py](scripts/compare_technical_logs.py).

| Metric | Baseline | Candidate |
|---|---:|---:|
| Wall time median | 4,377.813 ms | 173.038 ms |
| Process CPU median | 4,377.558 ms | 172.998 ms |
| Recursive sanitizer calls per steady sample | 1,066 | 20 |
| Python allocation peak (`tracemalloc`) | 35,377,622 B | 1,542,167 B |
| Returned records | 20 | 20 |
| Exact payload parity | — | **PASS** |

Both implementations decoded the same number of JSON records per sample: first call 1,077, subsequent calls 1,066. The first-call +11 is recorded as observed setup/decode work; it is not attributed to log records. Memory figures are Python allocation peaks, not process RSS. The copied input and sanitized response comparison files were temporary and removed at exit; only aggregate counts, timings, and the parity result are durable.

The optimized reader still decodes and filters the complete JSONL set to find the global newest 20 rows. The 173 ms total remains split only at the whole-function boundary; decode, filter, heap, and selected-row sanitization contributions were not separately measured. The pure log helper performs filesystem reads and JSON work only. The measurement did not observe provider HTTP in the exercised calls, and it does not establish a lifetime zero-provider-request claim. No CPU/GIL attribution for parallel API waits was measured.

## Checks and remaining gates

- `backend/tests/test_technical_logs_selection.py`: 6 passed using the project virtual environment and an isolated pytest base directory. Pytest emitted the existing unknown `cache_dir` configuration warning.
- `python3 tests/gates/gate.py validate`: PASS, manifest covers 150 test files.
- No production code, database, logs, config, provider, or runtime state was changed by attribution. The log implementation and test/manifest edits are source-only. No commit, deploy, or live verification was performed.
- Cold Health runtime breakdown should be revisited only through an explicitly bounded plan that does not invoke `dig` or other network probes. Do not optimize this path from the current nested timing alone.
- The `/servers` concurrent burst still needs direct queue/service attribution. Current evidence does not justify calling it an API queue defect or assigning it to background threads/GIL.
- Background/member/watchdog overlap and other roadmap Stage 4B gates remain open.
