# Stage 4 live resource and UI evidence

Date: 2026-10-05, Asia/Krasnoyarsk. Source baseline: `0b24cd1`. The commits from deployed application baseline `7478b02` through `0b24cd1` change documentation only. Selected deployed backend and UI files were SHA-256-equal to source at collection time. Service state was API, Mihomo, Xray, and Xray subscription gateway active. The effective global mode was `selective`, `selective_default=direct`, `server_mode=auto`, apply state `clean`; one provider binding was enabled. These are point-in-time facts.

## Safety and method

All observation was read-only. No provider URL was called, no subscription refresh, selector/apply, ping/probe, native validation, config generation, reconcile, restart, or explicit API mutation was run. The two UI requests visible as non-GET in the browser report are zero. Provider API request count observed by the UI harness was zero. Direct API measurements used only the read-only `/servers` projection (source documents that this path neither refreshes subscriptions nor checks ping) and the inventory GET. Inventory does read local runtime projections when `live_observations=true`; that matches ordinary UI behavior and does not initiate network probes/provider calls. SQLite state was inspected through a read-only URI.

A passive 150.7-second resource window sampled every 10 seconds (13 samples). The normal 60-second traffic collection timer ran during the observation period; no subscription refresh was due in this window. A separate 30-second passive storage counter window sampled the API process, its cgroup, and host SSD. Measurements are point-in-time and include unrelated host work. No rate extrapolation is made. The browser check reused the existing Chromium CDP session and did not install a browser. It performed one initial load, Settings/Admin view visits in both available locales, and no controls that change application state. UI network requests were captured by API path only; query strings and response contents were not retained.

Small samples are summarized with median, p95 (nearest rank; for five samples this is the maximum), and max. A p99 claim is not meaningful for these sample sizes. Browser resource durations are wall-clock request times and can overlap. The initial-load duration is DOMContentLoaded plus a bounded 900 ms settle wait; it does not prove every asynchronous panel finished rendering.

## Execution path findings

### Settings/Admin inventory

`UI view -> role selection -> GET /ui/settings/inventory -> display settings + health projection + traffic/subscription maps + role-filtered SQL -> projected rows -> UI render`.

The handler normalizes the requested role and builds `selected_kinds`, but calls `_subject_health_by_subject_for_ui(blocking=live_observations)` before it checks the selected role kinds. The default is `live_observations=true`. A cache miss runs `build_subject_state_projection(limit=1000)`, which reads all subjects and constructs runtime-enforcement, bypass, and override projections. The health result is cached for five seconds with single-flight, so concurrent requests share one cold projection rather than each doing that projection independently. Narrow role requests still wait for this shared cold work even when the role only returns a small system inventory. This path is a confirmed over-broad execution path; specific CPU attribution to the helper is unmeasured.

A bounded sequential sample of `role=router_core`, `limit=500`, `include_inactive=false`, `live_observations=true` returned HTTP 200 and 1,245 bytes in 2,483.8 ms on the first (cold) request, then 14.1, 11.1, 21.5, and 25.7 ms on four warm requests. Median 21.5 ms; p95/max 2,483.8 ms because the sample intentionally spans cold and warm cache states. This demonstrates a large cold/warm difference but does not isolate which sub-operation accounts for the cold time. Expected fix direction: after reviewing the health contract, make the health read role/subject scoped or defer health projection for roles/views that do not render it; preserve the existing canonical health source and avoid probes. Expected benefit is reducing cold narrow-profile latency and the concurrent UI wait. Risk: omitting or changing required health fields, or changing freshness semantics. Keep as a measured fix candidate; do not treat a source-level shortcut as approved without contract review.

The six inventory requests observed in the UI are not six duplicates: Admin explicitly loads six distinct role profiles in parallel (LAN, external network, VLESS, Docker, host, router core). Same-query duplicate behavior was not observed or measured. This fan-out can cause all six requests to wait on the same cold shared health projection, but the cache single-flight prevents duplicate full projection work during one cache miss.

### Other execution paths

- `/servers?inventory_state=active&limit=1000`: SQL inventory read, runtime logical-topology enrichment and local provider-member projection. Source explicitly states no subscription refresh and no ping checks. No provider network calls or probes were observed.
- `/api/v2/openapi.json`: one 100,866-byte request. The UI lazily fetches OpenAPI once per page to test API path support before compatibility behavior; this is not duplicate work in the observed flow. The schema is large relative to UI data calls. Treat the transferred schema size as a P2 measurement/consumer-audit item before changing the compatibility check. FastAPI's local `openapi()` generation path is synchronous on first use, but this measurement does not attribute CPU or event-loop blocking to schema generation.
- `/ui/router-summary`: three calls in the browser window. The service has a two-second process-local cache. Durations varied; tab/locale transitions and cache expiry mean duplicate/no-op work is not established.
- `/ui/external-ip` was not called because its handler performs two external fetches, one through Mihomo. `/health` was not used as a benchmark because a cold schema-state cache can call `initialize_database()` and execute schema/migration initialization. No `/diagnose` or `/reconcile` endpoint was called.
- Generation/readback/reconcile was not forced for this audit. The current generation's prior acceptance is documented by the preceding live milestone; generation-cost profiling needs a naturally occurring apply or an isolated candidate measurement.

## UI and API measurements

The current bounded browser run began in EN and changed locale to RU. It had no JS errors, no explicit mutations, and no Provider API requests. Initial navigation reached DOMContentLoaded plus settle in 1.421 s. It visited Settings and Admin in both locales at 1440x1000; the run was intended to measure initial request/render behavior, not mobile layout or row-count parity.

| API path | Browser count | Body bytes observed | Request durations (ms) |
|---|---:|---:|---|
| `GET /api/v2/ui/router-summary` | 3 | 2,820 aggregate | 2,605; 6; 222 |
| `GET /api/v2/servers` | 1 | 206,965 | 5,647 |
| `GET /api/v2/ui/settings/display` | 1 | 2,316 | 2,485 |
| `GET /api/v2/ui/settings/inventory` | 6 | 87,448 aggregate | 2,498; 2,592; 2,590; 2,584; 2,584; 2,602 |
| `GET /api/v2/openapi.json` | 1 | 100,866 | 1,579 |
| `GET /api/v2/events/recent` | 2 | 12,238 aggregate | one observed duration: 997 |
| `GET /api/v2/ui/settings/workspace` | 1 | not captured | not captured |

One sequential direct `/servers` profile sample (`inventory_state=active`, `limit=1000`) returned HTTP 200 in 110.1, 127.4, 120.5, 122.7, and 122.4 ms, with 204,322 bytes each time. Median 122.4 ms; p95/max 127.4 ms. This differs from the 5.647-second browser observation during concurrent panel loading; contention/overlap is a suspected explanation, not a measured root cause. Treat the browser measurement as a burst-window datapoint, not a stable endpoint latency.

UI body size is notable for the 207 KB server response and 101 KB OpenAPI schema. The direct server response was 204 KB. Compression transfer size, serialization time, and render time by component were not separately instrumented. No payload field removal is recommended from size alone because the response contract/UI consumers have not been audited here.

## Host, process, container, storage

During 150.7 seconds, the API process cgroup CPU usage rose by approximately 24.56 CPU-seconds (~16.3% of one core, ~4.1% of a four-core host). The API main process CPU rose by ~11.57 CPU-seconds (~7.7% of one core). This window included natural background work and the periodic traffic collector; it is not an idle baseline. No API cgroup CPU throttling was reported. Host memory was 15.4 GiB total with approximately 11.8 GiB available; API cgroup memory ranged 490.0–518.1 MiB with a recorded peak of 576.9 MiB. Main API RSS ranged 461.4–484.8 MiB. This short increase is a suspected trend only; it does not establish a leak. Host swap use was ~23 MiB.

Instantaneous Docker samples varied with load: Mihomo ranged around 135–137 MiB and Xray around 28.4–28.7 MiB; CPU snapshots ranged from ~0.1% to ~3% for Mihomo and ~0.02% to ~12% for Xray. These are single snapshots, not averages. Container PID counts were approximately 12. Container memory limits displayed as 15.4 GiB; no evidence of a tight container memory limit or CPU throttling appeared in this window.

The FWRouter database was 24,563,712 bytes at the start and 24,567,808 bytes at the end of the 150.7-second window (+4 KiB); the observed WAL and SHM files were zero bytes at each 10-second sample. During a later 30-second window the DB remained at 24,576,000 bytes and WAL remained zero. FWRouter log-directory tree size was 15,770,976 bytes at both ends of the resource window. These totals do not attribute writes to a specific timer/job. The host SSD (`sda`, 476.9 GB) recorded ~29.2 MB of block writes in the 30-second I/O sample, while the API service cgroup recorded +4 KiB device writes. The API process `write_bytes` counter rose ~2.0 MiB while `cancelled_write_bytes` rose ~2.1 MiB; these counters do not reconcile cleanly with cgroup attribution. Host block writes include other services. SSD write attribution and long-term storage growth therefore remain measurement-needed; do not extrapolate these short windows.

## Background jobs, timers, logs, runtime work

The traffic collector timer runs every 60 seconds and is on the watchdog signal path; it woke naturally during sampling. Subscription refresh runs every four hours with a randomized delay; it was not due during the resource window. Maintenance and jobs-retention timers were also not due. Service list showed API, Mihomo, Xray, and gateway active. No subscription refresh, runtime apply, selector decision, ping sweep, or provider request was started by this audit. Log directory size was stable during the passive resource window; journald volume and background wakeup CPU attribution were not separately sampled.

No Xray/Mihomo generation, readback, native config validation, or reconcile was forced. Current generation identity parity and no-op acceptance are covered by the preceding milestone report; the cost of generation/readback/reconcile remains measurement-needed until a naturally triggered or isolated-safe timing opportunity exists.

## Classification and next measurement

- **Confirmed issue:** narrow settings inventory requests enter the shared all-subject health projection before applying the requested role filter. Cold role request was 2,483.8 ms; warm requests 11–26 ms. The cache's single-flight avoids duplicate full projections during one cold concurrent burst.
- **Suspected:** concurrent initial UI panel loading contributes to the 5.647-second browser `/servers` duration, since the isolated sequential profile was 110–127 ms. Need component timing/queue correlation before assigning cause.
- **Suspected:** 24 MiB API RSS/cgroup growth over 150 seconds may reflect workload/cache growth; short sample does not establish leak.
- **Non-issue:** six Admin inventory calls are distinct role profiles, not duplicate same-key requests. OpenAPI schema fetch occurs once per page and supports existing API compatibility detection; no duplicate request was observed. Its 100,866-byte size remains a P2 measurement/consumer-audit item. Normal UI and tested projections made zero provider API requests; provider APIs were not queried.
- **Measurement needed:** SQL statement count/latency attribution, SQLite lock/wait attribution, compressed payload size and serialization cost, UI per-panel render completion, API process/cgroup I/O reconciliation, long-term SSD growth, log/journal write rate, background wakeup attribution, current in-process provider/cache counters, and native generation/readback/reconcile cost.

Detailed sanitized samples are in `LIVE_RESOURCE_SAMPLES.json`. No tests were run; this was production read-only measurement under the Stage 4 boundary, not a test gate. L6 was not invoked.
