# D-12 startup causality diagnosis

## Scope and evidence baseline

This is a narrowly scoped diagnosis, not a general performance optimization or deadline-policy change. PR #28 remains unchanged and Draft at `67af62af5ed91dc37371e9b1e6b34c7fbcec83eb`. Its eight documentation/history changes do not alter backend, tests, CI, installer or host code relative to accepted main `94724aaa221aac90af08c3affef7a59acae76de1`. Production and deployment runbooks are outside scope. No Stage 5/6A implementation is started.

Saved source-bound artifacts: main CI [39438911410](https://github.com/Coxf0rd/fwrouter/actions/runs/39438911410), PR CI [attempt 1](https://github.com/Coxf0rd/fwrouter/actions/runs/39444928657/attempts/1) and [attempt 2](https://github.com/Coxf0rd/fwrouter/actions/runs/39444928657/attempts/2). Main passed 2047/0/0; each PR attempt passed2046/failed1/skipped0. Exact source/plan/profile bindings and cleanup were verified; fixed/auto two-startup cases, qualified child and native Xray passed. The same scale case failed before steady load on both PR attempts.

## Three-run timing comparison

| Phase / metric | Main | PR attempt 1 | PR attempt 2 |
|---|---:|---:|---:|
| Ready wall / failure deadline elapsed (s) | 30.3792, ready | 45.0045, not ready | 45.0182, not ready |
| API process CPU at ready/deadline (s) | 23.39 | 34.51 | 34.48 |
| Global routing recovery wall / process CPU (s) | 7.3968 / 6.9604 | 13.5634 / 12.6148 | 13.6850 / 12.7910 |
| Xray profile recovery wall / process CPU (s) | 18.2683 / 12.3356 | 26.3745 / 17.4642 | 27.1309 / 18.1373 |
| Prospective bindings wall (s) | 2.1658 | 3.2778 | 3.3205 |
| Candidate staging wall (s) | 5.7446 | 9.0675 | 9.2473 |
| Two staged native apply calls, total wall (s) | 5.9922 | 8.3189 | 8.4043 |
| Publication finalization wall (s) | 2.0351 | 2.7292 | 2.9505 |
| DNS reconcile wall (s) | 0.9027 | 1.6194 | 1.6274 |
| Two global preflight calls, total wall (s) | 1.6380 | 4.6003 | 4.5446 |

These timers nest and cannot be summed as independent costs. Main retains only the first eight timed calls per phase; failed runs recover a bounded log tail, not a complete raw timeline. Xray publication/DNS values after the second deadline include diagnostic/termination time. CPU is process-wide, not thread-only; concurrent work can contribute.

The three preceding packet/fixed/auto cases were also slower in attempt1:60.571→73.179s,30.173→41.540s,21.302→29.802s. Main and failed runs share runtime/test source, synthetic counts (99,627 VPN rules,7profiles/70profile nodes/82synthetic native clients), locked dependency image/native binaries, nominal2CPU/2560MiB contour and test order. Source export excludes __pycache__, so main-only compileall does not establish a bytecode-warmup explanation. Matching runner labels/hostname do not establish physical-host identity or contention; device metadata differences are not I/O attribution.

## Traced execution and causal limits

FastAPI lifespan synchronously calls bootstrap before yielding. Bootstrap initializes owned directories/schema/jobs/subjects, then performs global routing and selector recovery, intended/scoped routing, Xray profile recovery and DNS reconcile sequentially. Schedulers, read-model prewarm and handler startup follow bootstrap. These later tasks cannot explain time inside bootstrap itself; their possible contribution to end-to-end readiness remains distinct. One bootstrap call is present; duplicate initialization has not been demonstrated.

Main thread snapshots show JSON decoding at different locations: effective-rules read during DNS reconcile in attempt1, owned Xray UDP receipt publication in attempt2. Subsequent output shows progress and phase exits. These observations do not establish deadlock or a uniquely stuck function. Xray receipt ownership callbacks deliberately repeat fresh fence/fingerprint/config/native checks; removing them without proof would violate the task. Current top-level timings do not split read/decode/hash/SQLite/lock/native costs at those boundaries.

At timeout workers remained alive and were explicitly SIGTERM-reaped by the harness. Failed lifetimes peaked at722728/733328KiB processHWM. That does not prove aggregate memory headroom or globally exclude OOM. Startup PSI/cpu.stat/io.stat/schedstat and cgroup events are absent; main load-wave OOM0 cannot be transferred to failed startup. Resource contention, CPU throughput/throttling, I/O/SQLite waits, allocator/GC costs and cache expiry/admission interactions remain unmeasured hypotheses.

## Existing instrumentation and bounded calibration

Timing wrappers execute in the measured process: locks/counter copies and flushed phase lines; validation wrappers separately hash candidate files before/after their phase timer; binding diagnostics perform identity checks inside their timer. Packet DNS observation also does an extra uncached effective-artifact read. Its cache-entry shape check expects two fields while current cache entries have three; cached-presence diagnostics are therefore not reliable cache-hit evidence. This does not authorize changing cache semantics or removing proof.

A bounded local calibration extracted the actual `observe_phase` AST and ran six isolated stdlib subprocess samples (three alternating pairs) on synthetic20MiB JSON read/decode/SHA256. Wrapped-minus-plain wall differences were−3.662,+0.682,+4.338ms; the effect is noisy and does not resolve a material sub-millisecond overhead. A separate additional-read/decode cost model for12.4MB synthetic rules-shaped data took92–112ms, but did not execute the real DNS observer. These are function-level calibration only, not native startup qualification or matched hosted measurements. No production inputs were used. Raw local samples and source hashes are retained privately.

## Bounded next measurement

A separate test-harness-only D-12 branch extends the existing RSS sampler to retain low-frequency bounded startup resource observations, including on timeout. It does not add a second monitor or change worker behavior, deadline, assertions, routing, CAS/fencing or native proof. Capture owned API CPU/scheduling/fault/I/O counters, the owned acceptance cgroup CPU quota/throttle/PSI/memory/OOM/I/O counters, monotonic timestamps and probe cost. Missing evidence remains null. No raw credentials, config, arbitrary paths or unrelated host/process inventory is emitted.

After source QA and regression checks, execute one source-bound full mandatory PR CI as the instrumented native diagnostic. This provides qualification and diagnostic evidence in one run; no blind repetition. If it fails or the cause remains unresolved, retain Draft/BLOCKED and report the exact missing attribution. If it passes, that qualifies the diagnostics package only and does not prove the original startup issue fixed. PR #28 is not promoted by this task.

## Current disposition

Root cause: UNKNOWN. Distributed slowdown and expensive serial bootstrap work are observed; attribution to an internal defect or external conditions is not established. No runtime optimization is proposed without evidence. Required45s ceiling, native proof and all safety gates remain unchanged. Further implementation depends on the bounded diagnostic results, not a sampled stack guess.

## Diagnostic package source qualification

The existing scale-test sampler now retains at most64 startup-only resource samples at approximately1Hz and freezes on `start_worker` return or exception. It separately measures probe monotonic wall/threadCPU cost, preserves nulls for unavailable/partial counters and exports the frozen record in metrics finally. Reads are bounded and limited to the already-owned API PID and its visible acceptance cgroup; there is no fallback to the host/pytest PID. Leader-thread schedstat is labelled explicitly; zero does not prove no scheduling wait. CPU throttling/PSI and I/O counters cover only the visible container, not hidden ancestor limits or unrelated host activity. Failure freeze can follow the harness diagnostic/disposal interval; the existing exact readiness deadline remains a separate metric.

Four added regressions exercise actual helpers against synthetic proc/cgroup fixtures, malformed/missing data, containment, no fallback, null versus observed zero, sanitized errors, bounded ring/frozen elapsed and success/failure export placement. Full local gate contracts:317PASS/0FAIL/0SKIP. Ruff global/changed static canaries, diff whitespace and monorepo surface PASS. Independent frozen source/docs QA PASSED; no production or native execution was claimed in that review.

A64-read synthetic probe calibration took22.50ms total wall /0.577ms maximum and22.51ms total threadCPU /0.572ms maximum; this local synthetic result is not hosted overhead proof. The instrumented run reports actual observed probe cost separately. No runtime/source owner, assertions, deadlines or CI limits were changed. PR qualification and diagnostic outcome remain pending until that exact frozen source executes in hosted CI.

## Paired causal experiment — bounded iteration, 2026-10-21

This iteration remains diagnostic and makes no runtime optimization or causal claim before measurements. The unchanged mandatory packet suite is followed, after strict aggregation, by exactly one independent supplementary packet suite on the same runner/source/dependency image. PR29-only orchestration retains supplementary receipts separately; it cannot replace the original mandatory result. Each suite uses fresh synthetic SQLite/native fixtures and a fresh worker process. OS page cache and host CPU placement are uncontrolled and explicitly labelled. Historical main is a reference, not a matched causal experiment.

Before each measured worker spawn, the pytest process runs three identical fixed JSON decode/SHA256 calibration samples, eight work units each, with a two-second cooperative budget checked between units. Incomplete calibration invalidates paired comparison and never alters the 45-second readiness deadline. Input manifests compare synthetic config/rules/Xray fingerprints and cardinalities. Runtime fingerprints, dependency digest, source/plan/profile bindings and distinct suite nonces are required.

Worker-only bootstrap counters aggregate JSON, SQLite, cache and known native/RPC boundaries by coarse phase; payloads, SQL parameters and URLs are not emitted. Nested timings are not additive. Bounded atomic snapshots survive a readiness timeout and explicitly identify the worker PID. Completed-operation counters cannot assign time to an operation still active at the snapshot. Coverage and observer overhead are reported, rather than treating missing evidence as zero. Comparison cannot establish a historical throughput change from the new pair alone.

After local regression checks, observer calibration and independent source QA, one full required CI containing this pair is authorized. No automatic rerun follows. The final disposition must be ROOT CAUSE PROVEN, PARTIALLY EXPLAINED or UNKNOWN; a passing sample alone does not resolve D-12. PR28, production, deployment documentation and roadmap stages remain untouched.

### Local paired-instrument qualification

The fixed parent-process CPU calibration completed three local attempts (24 work units each, 932,779-byte payload), taking80.242–87.257ms including setup. This does not calibrate the historical hosted runner. Five alternating synthetic observer-off/on pairs exercised12 decodes of a2,150,488-byte payload plus real stdlib SQLite/cache operations. After QA corrections, median observer delta was28.238ms wall /28.236ms threadCPU; wall range20.482–29.858ms. Relative overhead64.4–122.7% applies to this deliberately short cheap synthetic workload, not to the45s startup. Native startup overhead remains unmeasured until hosted execution. Actual encoding/hash, snapshot and bookkeeping costs are separately reported; sampling has a128MiB/128-sample cap. Truncation invalidates exhaustive repetition claims.

Initial independent source QA required two corrections: reject missing native/dependency fingerprints instead of accepting matching nulls; isolate actual SQLite callable time from SQL-template fingerprint and recorder overhead. Both are corrected with negative regressions. JSON observation also now has a strict inactive fast path before/after bootstrap. No runtime defect or optimization follows from these harness corrections. Raw calibration, exact helper hashes and local JUnit remain in the private server-only evidence directory. Final independent recheck and one hosted paired iteration remain pending.

Final local qualification:333PASS/0FAIL/0SKIP (four existing browser-mark warnings); the final suite-nonce binding follow-up separately passed all six pair/calibration contract cases. Ruff on all seven affected Python files, YAML parse, whitespace and monorepo surface checks PASS. Independent frozen source recheck found the prior findings resolved and no remaining material code findings; exactly one paired CI is justified as diagnostics, with overhead and OS-cache/host comparability caveats. Source review does not qualify startup or establish root cause. The required hosted result remains pending.
