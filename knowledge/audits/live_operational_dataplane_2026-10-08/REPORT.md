# Live Operational + Data-plane Benchmark

## Baseline, methodology and boundaries

Source baseline `6ba7f6f`; application delivery baseline `f78857f`. Measurement window: October 8 local time (+07), raw UTC timestamps October 7. This is an evidence-only milestone: no application optimization, new cache/index, parallel execution change or recovery-policy change. Existing standard installer and normal APIs are used for authorized operations. Tailscale/rescue remain independent and untouched. Protected online SQLite/config backup is recorded in `BACKUP.json`; do not restore the old database to undo a test client.

Detailed control evidence: [CONTROL_PLANE.md](CONTROL_PLANE.md); traffic evidence: [DATA_PLANE.md](DATA_PLANE.md); complete admission ledger: [COVERAGE.md](COVERAGE.md). Each raw JSON records actual observations, not fixture estimates. API acceptance is distinguished from verified job completion. Small samples do not establish production p99. Cgroup intervals contain observer/background work; `/proc` write bytes are process-accounted storage writes, not physical SSD amplification. CPU-time is not CPU peak. Missing SQL/lock/fsync/queue metrics remain unavailable, never zero.

## Timing baseline

| Operation | n | API/initial response | Verified/effective result | Class |
|---|---:|---:|---:|---|
| Health GET | 5 | p50 1.034 ms, max 2.745 | HTTP200 | live |
| System summary GET | 5 | p50 3.627 ms, max 1324.355 | HTTP200 | live, mixed natural-cache cohort |
| Auto state GET | 5 | p50 210.832 ms, max 213.166 | HTTP200 | live |
| Jobs GET | 5 | p50 10.801 ms | HTTP200 | live |
| Recent events GET | 5 | p50 171.702 ms, max 218.859 | HTTP200 | live |
| Settings workspace GET | 5 | p50 1.376 ms, max 356.716 | HTTP200 | live |
| Same-value Settings save | 3 | p50 4.259 ms | unchanged persisted value | live no-op only |
| Same-false auto-switch policy save | 1 | 8.184 ms | false, binding revision4 | live no-op only |
| Xray create, one owned identity | 1 | accepted 1112.782 ms | terminal verified 23642.365 ms | live |
| Xray delete, exact owned identity | 1 | accepted1124.724 ms | terminal verified21121.647 ms | live |
| Xray alias edit | 1 | 16.430 ms | identity/alias readback 4.826 ms | live metadata only |
| Backend same-baseline deploy | 1 | command64.115 ms | critical readback+293.463 ms | live, does not restart API |
| API restart | 1 | command365.104 ms | critical readiness44613.753 ms | live |
| Log single / ten serial records | 1 / 10 | 20.398 / 49.599 ms | 11 durable ordered INSERT/COMMIT pairs | live |

Fixed-route, delete and installer/API readiness receipts below separate successful state convergence from instrumentation limitations.

## Resource baseline

| Window | CPU time / peak | Memory | Writes / count | Limitations |
|---|---|---|---|---|
| 30 serial API GETs | API subtree 4.019 CPU-s | boundary 451.6→445.4 MB | unavailable | background/observer included |
| One Xray create | API subtree 27.114 CPU-s; CPU interval peak unavailable | sampled API cgroup peak 577.8 MB; Xray RSS 41.8 MB | API subtree 22.28 MB /1869 write I/Os | not request-only, not DB-only/SSD; runtime cgroup changed |
| One Xray delete | API subtree24.044CPU-s; sampled interval peak2.178cores | cgroup peak594.2MB; Xray RSS41.2MB | subtree23.84MB/2147writeI/Os | observer/background included; runtime counters reset |
| One +ten log records | producer CPU36.266 ms | boundary RSS36.36→37.63 MB | process write_bytes884736; 11BEGIN/INSERT/COMMIT | producer trace observed; no physical-device attribution |
| API restart interval | full CPU delta unavailable (epoch reset); sampled adjacent peak3.392cores | cgroup peak551.1MB; sampled main-process RSS503.8MB | full I/O delta unavailable | includes pre-restart oldprocess memory; not newprocess-only cost |
| Small traffic cohort | attributable CPU unavailable | sampled Mihomo/Xray/curl RSS in data evidence | host TCP counters only | no capacity/load claim |

Create sampler cost p50 0.819 ms/max25.847 ms at target100ms cadence; 40 job polls added 3022.693 ms cumulative handler response time. Observer overhead is retained rather than subtracted as if it were action cost. Short missed peaks and per-process incarnation changes are explicit limitations.

## Xray lifecycle and critical path

Normal create: admission → job → adapter candidate/native test → Xray restart → inventory/derived binding materialization → second candidate/native test → second restart → exact runtime presence/readback → terminal verified job. HTTP200 at submission is an accepted job, not fake verified success.

Docker event evidence proves two native-test containers and two SIGTERM restart cycles. Kill→die:196/170 ms; full kill→new start:471/411 ms. No SIGKILL or ten-second stop was observed. The first new runtime started at1.056s; the next candidate container was created at21.297s. The approximately20.24s intervening interval dominates create latency. Its exact DB/projection/native/lock partition is not directly measured: source tracing is not sufficient to blame one function.

The initial observer digest mismatch was a comparator defect (null handling, owned email identification and inserted user-rule positions), not a product regression. Independent semantic review confirmed all78 prior identities, all15 outbounds, non-user/global routing and per-user binding semantics. Evidence retains the failed comparator and its explanation. Do not delete a validation/restart stage merely because there are two: intermediate and final candidates differ.

## Data-plane results

Eight serial requests per Direct/Mihomo path and one two-request wave each:20/20 successful,4011 aggregate body bytes. Three independent HTTPS targets. Direct uses enp1s0 and bypasses proxy environment. Mixed-proxy timings include only local DNS/TCP setup; upstream DNS/CONNECT are not separately observable.

| Path | Serial n | Total p50 /p95 | Phase interpretation |
|---|---:|---:|---|
| Direct WAN | 8 | 345.1 /354.9 ms | DNS61.9,TCP85.9,TLS101.7,first-byte89.6 ms medians |
| Mihomo5201 | 8 | 226.7 /244.1 ms | local DNS/TCP near zero; CONNECT/upstream/TLS123.4,first-byte104.1 ms |
| Owned Xray rawWS | 1 | 216.0 ms | real ingress/auth+egress, server-local |
| Owned Xray publicTLS/WSS | 1 | 425.2 ms | valid outer certificate; local self-dial through NPM |
| Existing global/fixed handoff | 1 each | 272 /262 ms | exact applied egress only, not full existing-client ingress |

Five direct ICMP probes:0%loss, average57.477ms,mdev0.070ms. Host TCP RetransSegs delta0; unrelated host reset counters rose and cannot identify these benchmark flows. No flow-attributed reset/loss problem was demonstrated. UDP loss, sustained congestion, remote-client WAN ingress and robust RTT p99 remain unmeasured.

Four262144-byte downloads succeeded (1MiB aggregate). Controller/source-port correlation proves sampled proxy transfers used vpn-global/current member rather than DIRECT. Requested curl128K limit did not enforce instantaneous subsecond proxy rate: combined brief goodput~1.56MB/s. No further bulk test was run; this is application goodput, not link capacity or sustained overload evidence.

## Findings and future gates

1. **Confirmed high-resource, slow operations:** Xray create23.6s/delete21.1s with27.1CPU-s and22.3MB API-subtree writes in its observed interval. Need internal spans across the20.24s inter-stage gap before a fix. Source demonstrates sequential adapter and canonical binding stages, but not their independent removable redundancy.
2. **Confirmed repeated runtime actions:** two different-candidate native tests/restarts per create. Candidate/readback/rollback equivalence is required before consolidation; no stage is declared unnecessary solely by count.
3. **Confirmed logging transaction granularity:** 11events→11commits. Batch API/durability/order contract must be designed before batching; single-event durability must remain.
4. **Observed cache-sensitive reads:** system summary tail1.324s and workspace tail356.7ms with cheap warm reads. Handler/native/queue attribution remains open, no speculative cache/index.
5. **No demonstrated data-plane failure** in this bounded window. Faster local proxy path cannot establish processing overhead or remote endpoint capacity independently of upstream routing/DNS.

Parallelization candidates only: independent read-only projections/native validations after candidate identity and safety dependencies are established; none implemented. Batching candidate: explicit multi-event transaction contract, not transparent deferred logging. Consolidated client lifecycle requires one canonical candidate plus exact readiness/readback and preserved78existing bindings. Stage5 is not started by this audit; historical Stage4B closure is preserved.

## Unmeasured / staging-only gates

Shared globalAuto/manual server selection, Direct↔VPN, fullMihomo restart, changed subscription refresh, provider policy enablement/PATCH, destructive reset/retention and client batches are not executed to manufacture complete coverage. Current exclusive pool has oneProvider target. Existing fixed client bindings are not borrowed or changed. Auto membership effective-nochange behavior is not newly benchmarked here. No remoteMac workload, controlledUDP receiver, saturation throughput, full-cycle WAL/SSD amplification or exact API SQL/lock/event-loop spans.

Acceptance and final state/cleanup are recorded in the final receipt below. Test scope is L0 instrumentation/static/artifact checks plus necessary liveL4 readback, noL6/L7/application regression suite for this evidence-only milestone.

## Fixed-route measurement recovery

One normal subject-override operation was executed for the owned test identity only. Terminal job success/commit, clean override, Xray79/79applied+verified, no pending generation and all78existing client/binding/per-user-route semantics were read back. The shared Provider1456/exclusive/global target stayed unchanged. Docker history records one candidate validation and one runtime restart. Job field `runtime_state_unchanged=true` must not be interpreted as zero native runtime actions.

The observer failed after terminal/readback while summarizing resource rows (`KeyError: wall_ns`). No repeated POST was issued. Request latency, accepted timestamp and resource samples held only in the exited process are unavailable. DB job timestamps18:36:19→18:36:37UTC have one-second resolution; they are lifecycle evidence, not precise request wall latency. [Recovery receipt](XRAY_FIXED_ROUTE_RECOVERY.json) preserves the instrumentation failure and semantic confirmation. This is partial benchmark coverage with successful operation, not a product regression or fabricated timing.

The intended full owned-client fixed-route WSS GET was not measured: the temporary client harness failed its listener readiness check before sending a request, then removed its container/config. No repeated data probe was run. The successful fixed override/readback and earlier existing fixed handoff GET are separate partial evidence; neither is represented as the missing full ingress measurement. See `xray_fixed_route_probe.json`.

## Owned client deletion

The exact owned client was deleted once through the normal API: accepted1124.724ms; terminal success/runtime_verified21121.647ms fromrequest,19996.6ms afteraccepted. Thirty-six job polls consumed2490.417ms cumulative response time. Two candidate-test containers/two runtime restarts were observed. Readback confirms78existingidentities/bindings/per-user-route semantics preserved, owned client and subject absent,78/78applied+verified/no pendinggeneration, globalroute unchanged. API-subtree co-interval24.044CPU-s, sampled memory594.2MB and23.84MB writeI/O include observer/background; peak adjacent100.951ms interval accrued219.866msCPU (~2.178cores), not sustained CPU saturation proof. Raw numeric samples retained; sampler p50.799ms/max29.393ms. A report-print field typo occurred after raw save; no DELETE was retried. The immediate readback had one running-job projection; a subsequent read-only admission query confirmed zero queued/running jobs. The owned private identity was removed after cleanup gates passed.

## Deploy/restart/readiness and final acceptance

Exactly one same-baseline `installer/install.sh --deploy --component backend`:64.115ms command,293.463ms to post-copy critical HTTP readback. Installer itself does not restart. Exactly one explicit `fwrouter-api.service` restart:systemctl returned365.104ms; health+Xray78/78+selector+global critical readiness required44613.753ms. Connection-refused polls precede successful critical responses. This is an observed ~44s readiness gap, not a proven cause classification; no fake-success was claimed.

Resource observer retained444numericrows at target100ms; sampler cost p50.315ms/max35.318ms. Cgroup epoch reset means total CPU/I/O delta is unavailable. Adjacent100.980ms interval had342520usCPU (~3.392cores). Sampled cgroup peak551.1MB/main-process RSS503.8MB includes the old process before restart; it must not be presented as new-process startup allocation.

Restart overlap traffic remains unmeasured. Initial watcher expired before the marker was created; marker under `/run/fwrouter-v2` was also removed by systemd RuntimeDirectory cleanup. Only one idle Direct/Mihomo pair (271.753/213.821ms) was sent. No restarted operation or post-recovery surrogate was used to manufacture startup-traffic coverage. Its API process fields are invalid due observer systemctl-property parsing and excluded.

Final read-only snapshot:healthyAPI/schema24,auto revision75, exact original provenance/exclusive/desired-effective routing/Provider member1456 and bindingrevision4 preserved, nojobs/no failedunits, Xray78/78applied+verified, generated/mountedMihomo/Xray parity. Original binding-semantic digest matches final. Owned client/subject/privateidentity/harness resources removed. No forcedoutage, providerPATCH,global selection or rescue/Tailscale mutation. Point-window state continuity does not prove every unsampled instant lacked oscillation.

Quantitative provider evidence is epoch-aware: final same-PID normal critical GET cohort plus subscription metrics read has requests/discoveries/mutations/cache/error delta0 (`PROVIDER_POST_RESTART_BRACKET.json`). Process counters reset acrossrestart; never subtract before/after from different PIDs as proof of the whole action window. Membership/revision continuity is not provider-request accounting.

Acceptance verdict: safe live subset and cleanup PASS; full benchmark coverage PARTIAL by design and observer limitations. Residual gates are explicit. No optimization started after measurements.

## 2026-10-08 post-benchmark source clarification

[Subsequent attribution/future fix plan](../post_benchmark_plan_2026-10-08/REPORT.md) identifies that CRUD's own `runtime_verified` flag currently follows generated-host-file convergence after compose restart, not independent native-loaded identity proof. Separate benchmark native/parity/traffic acceptance above remains valid; no past live mismatch is inferred. This is a future lifecycle proof prerequisite, not a fix implemented in this measurement checkpoint.
