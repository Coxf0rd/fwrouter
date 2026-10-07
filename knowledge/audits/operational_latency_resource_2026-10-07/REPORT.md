# Operational Latency & Resource Audit — 2026-10-07

Source baseline `65c3024` (`65c302454038932021f25d2900b53fd0474e9db4`); deployed application `7de9f88`. **Measurement and attribution only. No optimization, application change, production deploy/restart, intentional production DB mutation, routing switch, subscription refresh, outage or provider mutation.** Natural background work continued during read-only observations.

## Verdict and coverage

Bounded live GET latency/resource observations, isolated installer/bootstrap/control-plane component measurements and network-isolated native validation establish a useful operational baseline. This is **not full live operational acceptance**: production restart, routing switch, changed-input generation and destructive lifecycle/resource peaks remain unmeasured. Source coverage does not substitute for execution. Detailed runtime coverage is in [RUNTIME_OPERATIONS.md](RUNTIME_OPERATIONS.md); isolated control-plane coverage and resource limitations are in [CONTROL_OPERATIONS.md](CONTROL_OPERATIONS.md).

No demonstrated P0 overload or routing regression. The strongest attributable resource cost is current full Mihomo native validation: 844ms including disposable-container startup, 500ms container CPU and 98.9MiB cgroup peak. `/system/summary` naturally spaced requests take 1.35–1.48s; warm consecutive requests can take approximately4ms. Cold/natural-refresh runtime enforcement is a latency candidate, not proof of excess work. Co-interval API CPU/I/O includes natural background jobs and cannot be charged to each GET.

## Methodology and measurement boundaries

- Measurements are serialized with an audit-only flock. No permanent collector or monitoring subsystem was installed. Existing Python/system interfaces and existing pinned local images were used; no package/image/browser installation.
- Live HTTP wall time begins after observer initialization and ends before observer finalization. API bodies are discarded; only response bytes/status/timing retained. Local HTTP timing excludes browser rendering, internet transport and interactive click scheduling.
- `/proc` API subtree sampling targets25ms; actual intervals and observer windows are retained. A short-lived child or sub25ms spike can be missed. CPU ticks have10ms resolution; zero sampled CPU does not mean no CPU work. Percent CPU uses one core as100%; this host has4 CPUs.
- Sampled RSS is current subtree RSS, summed shared pages included. RSS delta is versus each window start. Lifetime `VmHWM` and cgroup lifetime peaks are not operation peaks. Main-thread context-switch counters are not all-thread totals.
- Host load/runnable/PSI/network counters are contextual and background-confounded. Process `write_bytes`, SQLite statement counts, WAL size, file-tree bytes and physical SSD writes/fsyncs are different quantities. No physical write amplification or fsync count is inferred.
- n5 nearest-rank p95 equals max and is descriptive, not a population SLO. Heavy operations use bounded n1–3. No benchmark concurrency/load storm was created.
- Disposable validation containers: network none, no published ports, readonly root, dropped capabilities, CPU1, memory512MiB, pids128; copies private and removed. Current config contents/credentials are not evidence artifacts.
- Mocked runtime actions establish call order/counts and local overhead, not effective runtime latency. Native config validation is not live handshake, reload/readiness or exact active readback.

## Baseline timings

| Operation | Environment / n | p50 ms | p95/max ms | Meaning |
|---|---|---:|---:|---|
| Health GET | live serial /5 |1.406|4.117|HTTP200;312B |
| Servers limited active GET | live serial /5 |58.712|90.005|54,939B; not unrestricted inventory/UI burst |
| System summary, natural then warm | live serial /5 |4.316|1393.898|7,403B; mixed cache state |
| System summary, spaced6.1s | live serial /3 |1357.170|1482.538|natural cache behavior, no forced cache clear |
| Jobs limited GET | live serial /5 |10.864|12.707|7,102B |
| Events recent limited GET | live serial /5 |186.848|197.172|93,604B; category limits are not a93KB serialization-only benchmark |
| Selector GET, probes/state updates disabled | live serial /5 |253.386|256.217|3,834B; read model, not selection/apply |
| Backend+UI installer copy | private empty target /3 |59|60|48/59/60ms; excludes service actions/dependency installation |
| UI installer copy | private empty target /2 |13.5|14|static files; no build stage exists |
| Bootstrap fresh schema | private SQLite /1 |31.495|31.495|foundation only, not API readiness |
| Bootstrap warm schema | private SQLite /4 |9.299|9.522|131 SQL statements/call |
| Mihomo current config validation | isolated native /1 |844.031|844.031|Docker launch + validation;8,628,271B |
| Xray current config validation | isolated native /1 |247.637|247.637|Docker launch + validation;81,076B |

Additional local mutating component timings belong to [CONTROL_OPERATIONS.md](CONTROL_OPERATIONS.md); they must never be presented as production switch/apply latency.

## Observer calibration — limits on short-operation resources

No-op sampler n3 takes35.9–51.9ms wall and20–30ms quantized CPU, RSS delta0. Therefore isolated operations shorter than approximately52ms do **not** have action-attributed CPU/peakRSS from this sampler: tables show cohort+observer only. No subtraction can reconstruct a reliable sub-sample peak. Native owned-cgroup measurements and installer child getrusage are separate, more attributable sources. Fast HTTP timings remain measured independently. Per-process peak CPU, production fsync and lock wait remain unmeasured.

## Peak resources

| Operation | CPU / memory | I/O / concurrency | Interpretation |
|---|---|---|---|
| Mihomo current native validation |499.826ms CPU;98.9MiB cgroup peak|7 tasks;1.13MB reads/16KiB writes|attributable container cost; no throttle; capped1CPU |
| Xray current native validation |75.022ms CPU;9.28MiB peak|6 tasks;io.stat empty|empty accounting is not proof of0reads |
| Backend+UI temp deploy, latest |86.513ms child CPU;12,644KiB max child RSS|7,065,600 sampled write bytes;9,101,275 target bytes|brief multiprocess copy; CPU can exceed59ms wall; aggregate peak undersampled |
| UI temp deploy, latest |20ms child CPU;13,528KiB max child RSS|2,971,686 target bytes|short children missed; sampled0writes is not0I/O |
| Bootstrap cohort imports+5 calls |330.884ms CPU;43,792KiB process lifetime peak|2,699,264 sampled write bytes|includes imports/setup; per-call coldCPU19.192ms, warm7.253–7.850ms |
| Summary spaced, live co-interval |max171.07% one-core;688.9MiB tree RSS;98.2MiB RSS increase|up to5 subtree processes/41threads across all GET windows|natural background included; not request-only attribution |
| Events GET, live co-interval |max118%;625.3MiB RSS;41.7MiB delta|approximately187ms HTTP median|projection/resource candidate, not proved memory defect |
| Health GET, live co-interval |max83.16%;540.6MiB RSS;4KiB delta|HTTP median1.4ms vs observer window ≥41.6ms|background/observer interval prevents causalCPU attribution |

Across live windows: load1m≤0.93, sampled runnable≤13; CPU PSI avg10≤5.63%, IO/memory PSI avg10=0. API PID3481499 stayed unchanged. DB approximately24.2MB; sampled WAL peak0, which cannot rule out short WAL writes/checkpoints. Subtree observation duration41.6–1530.4ms differs from HTTP action duration. No measured sustained RAM/I/O pressure crisis; no claim of absent sub-sample overload.

## Critical-path attribution

Required reference chain: `action → admission/policy → DB → generation → validation → runtime action → readiness → readback → persistence → response`. Stages absent by design are N/A; unknown stages are unmeasured, not0ms.

- **Summary:** route inspects schema;5s summary cache; cache miss sequentially loads modules/Core bypass/scoped-egress runtime/system subjects; scoped-egress invokes canonical runtime summary. API/native readiness is a plausible cause of the warm/cold spread supported by the call chain. Per-stage production DB/native/serialization/queue timing is not instrumented, so no numerical partition is invented.
- **Servers/selector:** bounded GET measured, configured/effective intent preserved; selector GET explicitly disables on-demand probes/ping writes. These measurements do not time controller PUT, selection CAS or runtime transition.
- **Deployment:** standard installer deploy mode copies files and target bootstrap, skips packages/venv/systemd/network setup. Explicit service restart is separate, not hidden in copy timing.
- **Startup:** systemd preflight/import/lifespan foundation → log scrub/handlers/schedulers → prewarm → serving. Isolated foundation excludes schedulers/recovery/controller readiness. Full API restart and gateway dependency peaks remain a staging/next authorized delivery gate.
- **Changed generation:** admission/checkpoint/recovery/fingerprints → stage candidates → local/native validation → fenced transition apply/readback → Xray apply/incarnation/readback → final apply/readback → receipt/publication. Exact-byte same-operation validation reuse remains. Apply/readback ownership is not bypassed for benchmarking.
- **Unchanged reconcile/refresh:** current fingerprint gate skips candidate generation/native validation/restart when unchanged; ownership/readback requirements remain path-specific. Refresh itself was not triggered because it can contact provider and promote changed state.
- **Global Auto:** Core alone owns selection; snapshot/probes outside guard → guarded revision/intent/eligibility/incarnation revalidation → controller action/exact readback →CAS. Do not assume every ordinary selector change regenerates configs; detailed source path requirements differ from mode/fixed route/generation.
- **Provider recovery:** automatic-switch policy gate precedes discovery/candidate/PATCH. Unknown API evidence is notDOWN; ambiguous mutation is not retried blindly. No provider mutation/member switch benchmark.
- **Journal:** operational log writes SQLite plus sanitized JSONL; technical logs JSONL; read/filter projects bounded categories. Lifecycle logs and maintenance can overlap API timings. See isolated transaction/count evidence before labeling an extra write redundant.

## User, control-plane, effective and verified latency

Live GET gives local user-visible HTTP/control-plane response latency only. Native validation gives a safety-component duration. Mock apply/readback gives local call sequencing only. Actual effective routing time and verified readback timing for switches/restarts are unmeasured. Job-backed Xray lifecycle returns **accepted** before terminal verified completion by design; accepted is not success/convergence. Alias edit has a metadata-only path. No evidence here demonstrates false successful terminal response before verified runtime.

## Slowest / heaviest operations and trade-offs

1. Measured live summary natural-refresh: median1.357s/max1.483s; native/runtime aggregation must be partitioned before future fix.
2. Current Mihomo native validation:844ms,0.5CPU-seconds/99MiB; changed-input safety cost is real, unchanged-input no-op gates preserve avoidance. Dockerstartup~388ms and parse~456ms are sequential components of844ms, not additions to it.
3. Current Xray native validation:248ms,75msCPU/9.3MiB; startup~229ms dominates parser~19ms.
4. Selector read model:253ms, not selection action. Events read:187ms/94KB, possible projection allocation, unconfirmed causality.
5. File deploy:sub60ms but CPU86.5ms and severalMB writes. Fast wall time is not freeCPU/I/O; no host saturation demonstrated.

## Unnecessary work, candidates and non-issues

**Confirmed source work, cost attribution pending:** duplicate stale-job cleanup admissions in JobManager/create_job; bootstrap executes idempotent normalization and DDL even on warm schema; Settings save schedules deferred prewarm after the synchronous response. Their frequency/cost must be compared with required safety/normalization before fixing. This audit adds no cache/index/scheduling/guard change.

**Suspected:** long generation validation under Xray writer guard; cold summary/runtime refresh; GET window writes from background; journal projection allocation. No proven lock contention or SSD amplification. **Expected:** first native validation, exact runtime readback, revision fences, changed-generation safety; static frontend deploy; ordinary configured Auto membership remains editable under exclusive Provider and does not become effective eligibility. Temp canonical fixture disproved the suspected membership admission contradiction.

**Parallelization candidates, not implementation instructions:** independent readonly diagnostic projections after snapshot capture; owned immutable candidate validation after dependency/ownership review; independent postcommit read-model prewarm. Transition→Xray→final runtime apply is dependency ordered and must not be parallelized. Snapshot/revalidate/CAS and writer ownership cannot be relaxed to improve wall time. No safe parallelism is proved solely because two source calls are sequential.

## Provider request evidence

`PROVIDER_GET_BRACKET.json`: same-process before/after normal Health/servers/summary/readonly-selector plus subscription projection, request/discovery/mutation/cache/error counters unchanged. One historical targeted configGET remains cumulative; normal bracket delta0. `LIVE_GET_RESOURCE.json` contains empty provider-counter extraction arrays: **these are extraction gaps, not zero evidence**, superseded only for provider counts by the separate bracket. Native containers had no network. Isolated mocks assert0providercalls. No lifetime-zero claim.

## Unmeasured gates and future evidence order

- Real service restart/readiness/resource burst and lifecycle coupling; cannot derive from filesystem-copy or bootstrap measurement.
- Real changed/no-op refresh, full generation/apply/readback, global/manual/mode/fixed route switch and client batch lifecycle: source/safe analog only; disposable staging or next explicitly authorized delivery.
- Per-request production SQL counts/fsync/queue/lock wait and causal split of overlapping schedulers; host samples cannot supply them.
- Browser cold/warm/render/memory/user click latency; this audit's GET wall times do not replace prior UI evidence.
- Physical SSD/WAL amplification, storage growth and full timer cycle peaks; longer monitoring tail, not a demonstrated fix backlog.

Future order: (1) bounded stage timing in summary/runtime and source-confirmed cleanup/prewarm paths; (2) isolated/staging full lifecycle resources and guard holds; (3) fix only confirmed redundant work with before/after outputs/counters; (4) long-window WAL/SSD monitoring. Keep Stage5 separate; no reopening accepted Stage4B fixes without new evidence.

## Verification and artifacts

Audit helper syntax checks and owned-fixture assertions only; existing affected L0–L5 policy applies to any later fixes. No L6/L7 regression/fault injection. Native validations PASS for both current generated configs. No production deployment was performed. Detailed JSONs retain samples/counters and scripts reproduce safe scopes. Credentials/config bodies/native output are excluded. This report and future-priority roadmap updates constitute a documentation/evidence checkpoint, not application delivery.

## Isolated operation/count matrix

All SQL/resource counts are **cohort totals**; sampled CPU includes the observer, not authoritative operation CPU. Mock/deferred boundaries remain in CONTROL_OPERATIONS.md.

| Operation | n | p50 ms | max ms | SELECT total | DML total | COMMIT total |
|---|---:|---:|---:|---:|---:|---:|
| `display_settings_save_api` | 5 | 10.324 | 15.311 | 5 | 5 | 5 |
| `ordinary_auto_membership_toggle_service_mocked_reconcile` | 5 | 24.561 | 29.357 | 155 | 25 | 5 |
| `provider_auto_switch_policy_toggle_service` | 5 | 7.842 | 10.236 | 29 | 8 | 4 |
| `job_create_run_complete_noop_api` | 5 | 49.054 | 70.440 | 70 | 15 | 15 |
| `operational_log_single_db_jsonl_write` | 5 | 3.896 | 4.939 | 5 | 5 | 5 |
| `operational_log_burst_100_db_jsonl` | 1 | 442.634 | 442.634 | 100 | 100 | 100 |
| `operational_log_filtered_read` | 5 | 16.877 | 17.268 | 5 | 0 | 0 |
| `technical_log_burst_100_jsonl` | 1 | 12.871 | 12.871 | 0 | 0 | 0 |
| `log_retention_cleanup_temp_state` | 3 | 1.577 | 1.722 | 0 | 0 | 0 |
| `sqlite_small_transaction_commit` | 5 | 3.294 | 3.625 | 0 | 5 | 5 |
| `traffic_history_temp_retention_cleanup` | 3 | 1.170 | 8.108 | 7 | 2 | 2 |
| `xray_external_client_list` | 5 | 2.522 | 3.151 | 5 | 0 | 0 |
| `xray_private_route_intent_only_temp_db` | 3 | 10.214 | 12.430 | 16 | 7 | 6 |

| Operation | Cohort CPU ms | Sampled peak one-core CPU | Peak RSS MiB | RSS increase KiB | Accounted write bytes |
|---|---:|---:|---:|---:|---:|
| `display_settings_save_api` | 100.0 | 181.0% | 74.89 | 628 | 417792 |
| `ordinary_auto_membership_toggle_service_mocked_reconcile` | 180.0 | 134.8% | 76.40 | 1544 | 2285568 |
| `provider_auto_switch_policy_toggle_service` | 70.0 | 102.6% | 76.41 | 8 | 671744 |
| `job_create_run_complete_noop_api` | 320.0 | 148.8% | 76.98 | 584 | 2220032 |
| `operational_log_single_db_jsonl_write` | 30.0 | 125.2% | 77.00 | 24 | 331776 |
| `operational_log_burst_100_db_jsonl` | 390.0 | 245.7% | 77.11 | 112 | 6828032 |
| `operational_log_filtered_read` | 110.0 | 237.1% | 77.31 | 204 | 163840 |
| `technical_log_burst_100_jsonl` | 40.0 | 241.8% | 77.31 | 0 | 65536 |
| `log_retention_cleanup_temp_state` | 30.0 | 153.0% | 77.31 | 0 | 0 |
| `sqlite_small_transaction_commit` | 40.0 | 249.6% | 77.32 | 12 | 286720 |
| `traffic_history_temp_retention_cleanup` | 30.0 | 125.2% | 77.32 | 4 | 200704 |
| `xray_external_client_list` | 40.0 | 115.1% | 77.38 | 0 | 163840 |
| `xray_private_route_intent_only_temp_db` | 60.0 | 112.6% | 77.38 | 0 | 548864 |

Failed Xray create and missing-client edit/delete trials are excluded from successful operation timings; retained artifacts document the measurement gap. No-op JSONL retention is measured; expired-row rewrite/reset is not.

## Evidence integrity and reproducibility

Artifacts preserve original failed fixture cohorts rather than replacing them with successful lifecycle numbers. The first control harness was edited after capture (retention seeding and terminal error fields); its exact capture-version snapshot was not retained. Therefore current script is a safe reproduction aid, not a byte-identical provenance claim for that initial JSON. Later probes retain source/script/fixture hashes and independent evidence. Native helper cleanup was subsequently hardened in audit-only source without rerunning validation; results still describe the earlier measurement. No application code is part of this change.

Native total timing includes Docker start but container cgroup CPU/memory excludes Docker daemon/host launch overhead; imposed1CPU limit can differ from production. Private current-config copy size is8.63MB Mihomo/81KB Xray, removed after check. There is no quantitative live-network pressure attribution for mutating operations; host interface deltas include normal routing traffic.

## Requested-operation coverage ledger

| Requested operation | Evidence class | Runtime/native/provider actions measured | Remaining gate |
|---|---|---|---|
| backend --deploy | standard installer, private target n3 | production restarts0 | production copy/readiness peaks |
| frontend deploy/build | static installer n2; build N/A | native/runtime0 | browser live deployment readiness |
| API restart/startup | isolated foundation n5 + source | live restart0 | full lifespan/preflight/scheduler/readiness |
| Mihomo restart/reload | source + historical receipt | live restart0; current native check1 | actual reload/controller readiness/resource spike |
| Xray restart/reload | source + historical receipt | live restart0; current native check1 | actual reload/API readiness/resource spike |
| generation/validation/apply/readback | source chain; current config native1 each | native only; live apply/readback0 | complete generation/CAS/readback timing |
| global VPN-auto switch | Core source chain | live PUT/probe/switch0 | staged apply/effective/verified timestamps |
| ordinary/manual switch | source admission/Core/runtime path | live switch0 | staged selection and runtime |
| Direct ↔ VPN | source intent/apply path | live mode changes0 | staged mode validation/apply/readback |
| fixed/private Xray route | isolated intent persistence n3 + source | live changes0; no effective apply timed | separate generation/binding exact parity |
| subscription refresh changed/no-op | source gates/history | refresh triggered0 | changed+unchanged staging cohorts |
| reconcile | source fingerprint no-op/changed chain | production reconcile triggered0 | guard hold/queue/actual callback cost |
| settings save | isolated API n5 | deferred prewarm stub5 | real prewarm completion/resources |
| Auto membership toggle | isolated configured intent n5 | mock reconcile5; provider0 | actual effective-set change apply |
| provider auto-switch policy | isolated setting n5 | provider operation0;4writes/1no-op | none for local setting scope; UI/live mutation not run |
| Xray client create/edit/delete/list/batch | isolated attempts + targeted probe, companion report | no production mutation | mock terminal outcome != actual native convergence |
| Xray enable/disable | no standalone endpoint identified |0 | do not invent unsupported API; lifecycle uses create/delete/route |
| journal one/burst/read/filter | isolated1/100events + live recentGET |100commits/burst;0native/provider | production-scale fsync/durability pressure |
| cleanup/retention | isolated no-op JSONL + expired traffic row |0native/provider | full expired JSONL rewrite/data reset |
| data/statistics reset | source/isolated retention only | destructive reset0 | disposable staging; no reset equivalence claim |
| DB transaction/commit | isolated n5 |1INSERT/commit per call | production fsync/contention |
| job/event create/complete | isolated noop n5/events cohorts |5jobs/15commits;0native | scheduling contention/real workers |
| startup/background bootstrap | isolated schema+source schedulers |131SQL/call; live startup0 | complete scheduler cycle/overlap/WAL |
| other reads: Health/servers/summary/selector | live serial bounded GET | provider bracket delta0 | request-only SQL/native/serialization attribution |

Counts are audit actions or isolated observed calls, not lifetime service totals. Unknown live native counts are unknown, not zero. Full paths may generate multiple candidates; the1native value refers only to each standalone current-file validator.

## Xray client lifecycle — successful isolated follow-up

Original failed attempts remain in CONTROL_PLANE_METRICS.json. Audit fixture was corrected at Xray status adapter and DNS host-probe boundaries only; all real subprocess/socket operations stayed denied. Successful results below use synthetic validation/reload/readback, **not native or production convergence**. Batch means three sequential creates/deletes, not concurrent pressure. Separate enable/disable endpoint was not identified.

| Operation | n | p50 ms | max ms | Sampled peak RSS MiB | RSS increase KiB | Cohort accounted writes |
|---|---:|---:|---:|---:|---:|---:|
| create job to terminal | 3 | 279.064 | 286.922 | 75.84 | 2444 | 10739712 |
| delete job to terminal | 3 | 264.129 | 272.606 | 76.00 | 116 | 7348224 |
| alias edit | 1 | 15.443 | 15.443 | 75.84 | 0 | 212992 |

Three creates total action wall 826.559ms; three deletes 799.258ms. These sums exclude between-action observer setup/finalization and list lookup, so they are not whole-batch elapsed. Final fixture inventory0; observed fake runner reloads12; real native/provider/restarts0. Per-operation CPU includes observer; exact accepted-to-effective/runtime verified timing remains unknown.

Latest durable calibration is35.895/44.810/51.904ms wall,20/30/30ms CPU, RSSdelta0; use this final artifact rather than earlier transient calibration numbers. Fast operation resource attribution is therefore unreliable below roughly52ms in this harness.

Host instantaneous CPU peak and per-process CPU peak breakdown were not captured reliably; CPU PSI/runqueue and subtree/container CPU are different metrics. No fsync tracer was available and none was installed. Queue/scheduling/lock contention numerical partitions remain unmeasured. No safe parallelization change is approved by this evidence; candidates require snapshot/dependency/fencing review first.

## Client reload call attribution

The12 fake reloads are source-attributed to two pairs per create/delete: adapter create/delete performs test+reload, followed by `materialize_xray_runtime_bindings` test+reload. Three creates plus three deletes produce12; alias metadata edit does not reload. Per-operation call snapshots/config hash equivalence were not captured. This is confirmed two-stage work in the synthetic path, **not yet proof of redundant native reload**; staging must compare intermediate/final config semantics, lifecycle ownership and native readiness before proposing targeted reload/coalescing. API acceptance vs terminal timestamp split and follow-up SQL counts were not captured.
