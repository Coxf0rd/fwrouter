# Post-benchmark attribution and proposed fix plan

## Scope and decision

Audit baseline: `eb77ebea514cf7e3966aad75171ff4e111f4bc8f` (application remains the `f78857f` delivery). This checkpoint reads source and retained evidence only. No new live probe, restart/deploy, database/provider mutation, benchmark, application/instrumentation edit or test execution. The [live benchmark](../live_operational_dataplane_2026-10-08/REPORT.md) remains authoritative for measurements. The [canonical roadmap](/решения/roadmap/fwrouter/ROADMAP.md) is the sole execution plan; this report defines the evidence gates for three proposed packages, not permission to implement them now. Stage 5 remains deferred.

**No measured P0 outage/overload is established.** P1 priorities are startup readiness and Xray lifecycle latency/resource cost. A separate P1 source-level Xray terminal proof gap must be closed before lifecycle consolidation; it does not establish a past live failure. P2 covers measured logging transaction granularity, unchanged scrub temporary writes and cold summary attribution. Missing telemetry is an attribution gate, not a zero-cost result.

## Baseline and attribution limits

| Finding | Retained live measurement | Attribution now established | Still unknown |
|---|---|---|---|
| API readiness | restart command365ms; critical HTTP ready44.614s | synchronous lifespan bootstrap/recovery/scrub precedes HTTP serving; Type=simple is process-start, not ready | per-stage44s partition, locks, import/preflight/CPU/background contribution |
| Xray create/delete | verified23.642/21.122s; accepted1.113/1.125s | adapter identity mutation then inventory/canonical bindings then second candidate/restart | per-stage timing/CPU/DB contribution |
| Create inter-stage gap | first restart complete1.056s; next native candidate21.297s, gap20.24s | inventory/alias/profile-or-binding projection/Mihomo handoff prepare intervene | which operation dominates; no internal20s fixed wait found |
| Two tests/restarts per create/delete | observed2+2 each | two different candidates are applied; compose restart is real process restart | whether one canonical candidate can preserve all failure/rollback semantics |
| Lifecycle resource pressure | create27.114CPU-s/577.8MB cgroup peak/22.28MBwrites; delete24.044CPU-s/594.2MB/23.84MB | API subtree plus workers/polls/background, not pure handler; sampled peaks/counts | attribution to each projection/native/DB phase and physicalSSD/fsync amplification |
| Log commits | 11events→11INSERT/BEGIN/COMMIT; single20.398ms, burst10=49.599ms | one transaction per standalone event, JSONL after commit | benefit at representative production event volume; batch durability/order contract |
| Summary | n5 mixed-cache p50 3.627ms/max1324.355ms | schema inspection then5s cached summary; cold projection/runtime checks, existing enforcement reuse | handler/queue/SQL/native/serialization partition and overlap |
| Collector | saved Pass1 timer fires before API port ready, next regular tick recovers | After on Type=simple does not gate HTTP readiness | gateway-specific restart count and traffic continuity during startup |
| DIRECT vs proxy | n8/path totalp50 345.1/226.7ms;20/20shortGETs pass | Direct WAN DNS/TCP vs local proxy setup/upstream route are different contours | no router-processing cost delta, remote ingress, robust loss/capacity or DNS cause proof |

Readiness peak551.1MB/cgroup and503.8MB/MainPID RSS include the old process; CPU/I/O counters reset. Do not turn them into new-process startup allocations or cross-incarnation deltas. Create40/delete36 job polls and their3.023/2.490s aggregate response times are observer work, not additive critical-path stages. Source count opportunities are not measured saved seconds.

## Actual critical paths and mandatory boundaries

### API startup and followers

`systemd preflight → uvicorn/import → lifespan bootstrap → dirs/schema/stale-job handling/normalization → guarded live and intended/scoped/Xray recovery → dnsmasq reconcile → credential scrub → handler registration → scheduler start + background prewarm launch → lifespan yield → HTTP → critical runtime/readback`.

Source: `main.py:90-107`; `bootstrap.py:883-927`, `647-709`, `710-852`; API unit `host/systemd/fwrouter-api.service:6-20`. Startup Xray path may reconcile subscription generation then final Mihomo; this is a candidate for repeated projection work, not permission to omit required recovery. Schema/init, crash checkpoint recovery, intent/runtime fencing, secret redaction and honest readiness are mandatory. Do not simply background bootstrap and publish healthy while owned recovery remains unresolved. First partition and reduce proven no-op work within the current order. Splitting core-ready from runtime-ready would require an explicit new public admission contract; it is not this plan's default fix.

`prime_runtime_read_models_async` (`runtime_prewarm.py:10-55`) is a daemon thread and does not itself synchronously await its whole work before HTTP starts. Immediate maintenance/convergence/other scheduler ticks and its prewarm can overlap; actual cost not retained. External collector's initial interval wait is not a startup fixed-delay workaround.

Traffic follower:60s systemd timer → collector unit After/Wants API → curl POST,40s max → job outcome. API Type=simple can be active with no listening port. Gateway has its own bounded TCP port gate (`fwrouter-wait-port.sh`) but TCP availability is not full domain health. Preserve independently useful follower behavior; do not infer Docker→API mandatory startup coupling or replace it with sleep.

### Xray create/delete

Create: identity/admission lock + preflight → accepted job → adapter candidate add → native test → active write/restart → inventory/alias and optional profile reconcile → collect bindings/modes → prepare Mihomo handoffs → second Xray candidate/test/restart → binding/mode config assertion → identity config convergence → audit/export → terminal job.

Delete: exact identity/admission lock → accepted job → adapter remove/test/write/restart → inventory sync/scoped subject/override cleanup → collect bindings/modes/handoff prepare → second candidate/test/restart → identity absence/config assertion → terminal job.

References: `routes/xray.py:284-305,331-347`; `xray_clients.py:137-269,367-474,478-592,641-702`; `adapters/xray_real.py:81-108,854-979,1283-1395`; `xray_materialize.py:372-531`. Alias is metadata-only and is not a runtime optimization target.

Required: identity uniqueness, managed-runtime admission, source/intent snapshot and serialization, native test of the exact candidate, required listener availability, last-good/fenced failure semantics, scoped persistence, original fixed routes, mounted/loaded proof and honest terminal result. Never reuse a validation across different candidate bytes, runtime binary/image or security/runtime capability. Existing equality/force_reload branch (`xray_real.py:1334-1351`) only skips identical materialization; it does not prove the first distinct candidate is redundant.

### Terminal proof gap (P1 contract prerequisite)

`verify_xray_client_runtime_convergence` (`xray_materialize.py:22-49,303-368`) reads adapter.config_path on the host, checks client presence/absence and stale routing rules. `_verify_active_config_bindings`/mode checks also read this file. Default adapter reload returns Docker Compose restart result; CRUD workers then declare `runtime_verified=True` (`xray_clients.py:464-474,694-702`). This chain does **not itself** prove that the restarted process loaded the expected identity/config. No failed loaded state was observed in the benchmark: separate final/native/traffic checks passed. This is an overbroad source proof label, not evidence that accepted HTTP was fake-success.

Future lifecycle must reuse existing adapter `list_loaded_client_identities` (`xray_real.py:731`), `get_runtime_incarnation`/`get_runtime_config_sha256` (`782,819`) and existing fenced generation readback machinery (`xray_subscription_service.py:1499-1505`, etc.). Require exact identity set/candidate digest and stable incarnation, then binding/mode persistence and terminal success. Config-present but native-absent/unavailable must remain failed/unconfirmed, never verified. No new parallel state machine or blind rollback; do not add a handshake to every CRUD operation merely to replace exact native readback.

## Redundant-work candidates with source proof

| Candidate | Source/cost mechanism | Classification and safe contract |
|---|---|---|
| Per-client mode projection before filtering | `xray_bindings.py:106-142` calls `get_subject_with_effective_state` per explicit client; `subject_policy.py:721-735` rereads subject/detail/routing/bypass/preflight/overrides | source-confirmed repeated work; batch shared snapshot/overrides, preserve enabled→user-override Direct; not all modes may be cheaply discarded |
| Binding collection repeated for handoff | `xray_materialize.py:386-393` collects binding/mode then Mihomo reconcile can collect bindings again (`mihomo_config_inbounds.py:125-150`) | share only same operation immutable snapshot with revision/incarnation revalidation; preserve existing listener retention; no global cache |
| Subject N+1 | `xray_bindings.py:88-102` calls get_subject per row; subject/detail and overrides independently fetched | source-confirmed repeated reads; use existing batch projection mechanisms after parity/count tests; cannot claim it caused20s yet |
| No-change log scrub temp write | `event_contract.py:174-221` creates/writes/fsyncs full temp then unlinks if unchanged; startup `main.py:93-98` invokes scrub | source-confirmed write amplification; keep full sanitization scan, lazily create temp only at first changed line; preserve malformed/secret handling/atomicity/permissions |
| Two lifecycle candidates/restarts | raw identity mutation then canonical routing materialization | measured repeated actions; consolidation candidate, not redundant under current contract; needs staged failure/readback proof |
| Separate config readbacks | binding verifier, mode verifier and client verifier parse same path | snapshot parse may be reused only with same digest/incarnation; required assertions remain; do not merge away external readback |
| Logging singleton commits | `logs.py:156-278`, measured11commits | intentional durability today; explicit batch producer contract only, existing single event stays synchronous/durable |
| Cold summary checks | `system.py:64`, `system_summary.py:47-128` | latency confirmed, redundant work not established; existing enforcement reuse/cache is already fixed, do not reopen it blindly |

`_default_subject_runtime_enforcement` calls preflight with `require_runtime_verify=False`; health uses existing short cache. This is not one full native validation per row. SQLite `db_session.commit()` for a read-only transaction is not evidence of a WAL write/fsync. `connect()` performs filesystem permissions/PRAGMA work on many connections (`db/connection.py:25-80`); measurement gate only, no speculative pooling/index/schema rewrite.

## Sleep, polling, retries and sequencing audit

Static AST scan found nine production Python sleep sites:

| Site | Actual semantics | Classification |
|---|---|---|
| `bootstrap.py:269-298` | only selective/VPN waits for missing dataplane,30s deadline/0.5s poll | bounded evidence readiness; not unconditional30s or proven44s cause |
| `mihomo_runtime.py:103-141` | controller health until running,20s deadline/0.5s poll | bounded observation, not repeated restart |
| `xray_materialize.py:303-368` |3s generated-config convergence poll/0.1s | bounded but proof scope too weak; not20s fixed wait |
| `adapters/xray_common.py:54` | bounded flock contention retry up to deadline | required writer acquisition; contention unmeasured |
| `jobs/manager.py:128-163` | queued start≤0.5s/20ms; bounded job-status wait/.2s | accepted-job response observation; no re-execution of mutation |
| `db/connection.py:41-52` | up to6 WAL PRAGMA attempts only on database-is-locked, busy_timeout30s | bounded explicit lock handling; unmeasured contention, not arbitrary replay |
| `apply.py:391` | max3 delayed read-only live-mode probes after mismatch | bounded verification retry, not blind PUT/apply retry |
| `dnsmasq.py:504-513` | selective-status retry after actual restart | evidence polling; no normal fast-path fixed sleep |

Gateway `fwrouter-wait-port.sh:26` is boundedTCP readiness; scheduler Event.wait and systemd60s timers are cadence, not fabricated readiness. No unconditional20/44s sleep or blind providerPATCH retry was found in the traced lifecycle. This is a scoped source result, not a universal proof about all repository integrations. No speculative timer/cadence removal.

The relevant work is mostly synchronous calls/worker threads, not a chain of independent async awaits. Dependencies include intent before candidate, handoff readiness before outbound application, native validation before apply, native readback before confirmed persistence/publication. These remain serial. Possible parallel-only candidates: pure projections or isolated validators for independent immutable artifacts after shared snapshot capture; bounded CPU/I/O concurrency budget and stale-snapshot rejection are mandatory. Running parallel validators can shorten wall time by increasing resource peaks; resource efficiency, not wall time alone, decides acceptance. No parallelization of common writers/recovery/final CAS or same candidate stages.

## DIRECT versus Mihomo interpretation

Direct median includes WAN DNS61.9ms/TCP85.9ms/TLS101.7ms. Proxy curl DNS/TCP are local (~0/.17ms), while CONNECT/upstream/TLS123.4ms are combined. Different upstream/DNS/route reuse prevents attributing118ms difference to FWRouter inefficiency. No DNS/MTU/firewall/selection optimization is justified. Same destination/IP/SNI, comparable cache state and a real remote client with controlled receiver are measurement prerequisites; no provider switch or broad routing mutation for this comparison.

## Proposed packages and execution order

### Package 1 — lifecycle attribution, startup admission and write-only no-op correction (P1/P2)

1. Bounded, secret-free monotonic spans and counts for import/preflight/bootstrap subphases, Xray projection/handoff/materialization, cold summary and first scheduler ticks. Record SQL classes/counts/commits, file/temp bytes, native commands, guard wait/hold, PID/cgroup epochs and observer cost; no full payload/SQL text. Correlate operation_id/job/candidate digest without new event/state subsystem. An isolated representative replay precedes a separately authorized single live measurement.
2. Fix readiness-aware traffic collector admission: prove current failed-on-startup behavior with fixtures, skip/defer only when API is unready using a typed reason and existing periodic trigger; no synchronous delayed restart loop or false completed collection. A change to systemd-notify/API-wide readiness is not the default minimal fix.
3. Optimize proven no-change scrub temp writes while retaining scan/redaction; preserve security/caller paths and atomic write on actual change. No startup task is moved to background to hide latency. If spans identify additional expensive no-op startup work, return to review rather than widening the package.

Expected benefit: remove proven unchanged-log temporary writes and false collector failures; determine exact44s/20s cause. No promised44s speedup from instrumentation. Risk medium (startup/log confidentiality/admission). Required affectedL0–L4 plusL5 for shared lifecycle/events contracts: bootstrap/event-contract/runtime-summary/job/collector-wrapper; secret/malformed/corrupt/repeated scrub, mutation-disabled readiness, idempotence and no new provider/native calls. DisposableL7 for restart/recovery/unready collector; no production fault injection. Compare wall-to-listen/critical-ready separately, stage CPU/RSS/tempwrites/native/WAL counts, early background load and negative call assertions. Persist marker outside the restarted unit's RuntimeDirectory; fix observer parse/epoch errors before another benchmark.

### Package 2 — Xray canonical lifecycle proof and measured work consolidation (P1)

First close CRUD terminal native-loaded proof using existing generation/adapter mechanisms. Then reuse operation-scoped batch subject/override/enforcement projections; preserve all Direct/Disabled/selective/expiry/fixed/global/legacy semantics and current Core ownership. Use Package1 spans to rank remaining costs. Only after equivalence evidence, design one candidate incorporating identity+managed bindings/modes, one tested digest/one apply and complete native-readback/CAS terminal receipt. Raw adapter writes must not race canonical materialization. Do not merely suppress reload #1 or validation #2, and do not reapply old intent on conflict.

Expected benefit: remove repeat local projection/file parsing and, conditionally, one candidate test/restart per create/delete; avoid one intermediate connection interruption. The20s gain is a hypothesis until stage timings, not a target guarantee. Native-loaded verification may add necessary work. Risk high (client admission/routing/publication/recovery). Required affectedL0–L5: test_xray, test_xray_native_readback, generation_recovery, vpn_auto_lifecycle, reconcile_xray, fixed bindings/public export/Disabled+Direct+enabled override contracts; CAS/incarnation/intent interleavings and native/config divergence must be deterministic. DisposableL7: crashes between intent/write/restart/readback/persistence, stale/newer candidate, failed native/restart/partial restore, idempotent recovery and sustained existing streams. Live only approved dedicated singleton with backup/exact cleanup after staging passes; existing78bindings,Provider/exclusive/provenance/no polling unchanged. Measure create/delete accepted/effective/native-verified wall; per-stage CPU/RSS/reads/writes/commits/tempbytes; native count/restart/connection interruption; test1 vs representative batch in staging only. No performance change promoted if required native proof is unavailable.

### Package 3 — measured read-path and explicit producer batch efficiency (P2, conditional)

Use retained spans to select only confirmed cold-summary duplicate work; preserve current schema/runtime health freshness,role filtering/cache bounds/single-flight/fallback and exact output. No speculative new cache/index. Separately expose an explicit bounded synchronous event batch only for a demonstrated multi-event producer, through current logging/transaction contract. Single event retains commit-before-JSONL; batch needs stable ordering/event IDs/sanitization, rollback/no publication on failure, bounded memory and clear JSONL failure behavior. Existing caller-owned transaction event writer is reused where appropriate. Do not add timer-based asynchronous flushing or count every log as redundant.

Expected benefit: fewer commits for actual bursts and lower measured cold-read overhead; no single-log latency claim. Risk medium (durability/ordering/Health). AffectedL0–L4+L5 events/Health/shared API where applicable: events correlation/visibility/admin audits/technical logs/summary/state tests; deterministic concurrent ordering and commit/JSONL failure tests. DisposableL7 for audit-writer crash/durability window if contract changes; readonly same-PID live summary/provider bracket and normal logged operation after approval. Compare n1/n10/n100 isolatedmatched event payloads/commitcounts/CPU/WAL/JSONLbytes, cold/warm summary separately and queue/native stage counts. If cost/redundancy is not demonstrated, keep as measurement tail, not implement.

Packages execute1→2→3; Package2 instrumentation can be prepared with Package1, but lifecycle semantics cannot be mixed into the instrumentation/readiness commit. Each coherent package may use source and subsequent evidence commits; no giant cross-domain fix commit. Independent-stage concurrency is only a later reviewed candidate after resource comparisons, not a fourth automatic package.

## Safety and acceptance gates kept open

Shared globalAuto/manual switches, Direct↔VPN, fullMihomo restart, changed subscription refresh, provider-policy enablement/PATCH, destructive reset/retention, failure/recovery/batches: disposable staging first. No live forced outage or alteration of Estonia/other fixed bindings. Current exclusive pool has one Provider target; do not create an alternate to satisfy benchmarking. UDP loss/sustained throughput/remote ingress and startup data-plane continuity remain unmeasured. Gateway actual restart count was not captured. Provider same-PID postrestart GET delta0 proves only that normal read window; previous and next PIDs are not one accounting bracket.

Every implementation records Source/Tests/Commit/Deploy/Live separately, comparable before/after and exact baseline failures. AffectedL0–L5 per current contract; no defaultL6/full suite; L7 only disposable release/staging. No real tests run for this documentation-only audit; documentation/static/reference checks only. Existing completed provider/protocol/recovery/Stage4B gates are not reopened absent new evidence. Original reports retain their accepted point-time scope; the new terminal-proof finding narrows what CRUD source alone proves, not their independent live native checks.
