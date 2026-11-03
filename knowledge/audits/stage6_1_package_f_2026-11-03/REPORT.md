# Stage 6.1 Package F — Application Lifecycle & State Ownership

**Status: implementation acceptance VERIFIED at `35ed543295b91363b06a6c69593c2774b7d3f60a`.** Source QA, exact-HEAD push-fast, complete qualified branch L6 and independent offline evidence verification passed. This report records those actual results. The report-only publication commit must receive its own exact-HEAD checks; final publication receipts and overall Package F closure are recorded in the dated owner-local [canonical roadmap](/решения/roadmap/fwrouter/ROADMAP.md). No production data, deployed native runtime, or system service was accessed.

## Source identity

- Repository: `/root/fwrouter-approved-main`
- Branch and baseline: `stage6/backend-completion` at `7800ea857d662f718164f0b2a36872678135e235`
- The approved baseline is reproducible from the exact Git commit above; the final candidate source and documentation digests are listed in [SOURCE_SHA256.txt](SOURCE_SHA256.txt), SHA-256 `84aae00e1068b6f75882e5f05c2a5114a8942be0bf65db51b971a78521e8b03f`. It covers 31 changed source/test/knowledge files and excludes this report, itself, and the protected repository roadmap pointer.
- The only pre-existing change remains `knowledge/ROADMAP.md`; it was preserved byte-for-byte. At candidate freeze, the canonical roadmap was unchanged. Its final dated Package F closure is an owner-local append, preserving every prior byte and unpublished Stage 6A decision.

## Before and after ownership

| State or responsibility | Before | Package F candidate |
|---|---|---|
| Persistent Core intent and applied state | SQLite database and existing Core/apply owners | Unchanged. No database, schema, admission, transaction, or persistence changes. SQLite remains authoritative for durable intent. |
| Settings and paths | Process-cached `get_settings()` plus `Settings.paths` deriving from the current environment | Unchanged values and lookup behavior. Runtime remains one API process over one configured state-root lifetime; changing state-root environment during active work is not reconfiguration or isolation. |
| Application composition | `main.create_app()` built routes but closed over module-level settings, singleton manager, schedulers, prewarm and fixed lease without an explicit lifecycle owner | `ApplicationLifecycle` now owns ordered startup/shutdown coordination. A narrow lifecycle factory is resolved only after entering the fixed host lease. Production composition uses the same existing module facades and process owners; test injection does not claim an isolated route/database/runtime universe. |
| Default JobManager registration | A module boolean tracked one global manager's built-in handler registration; concurrent first callers could both observe `False` | Registration completion and a separate registration lock now belong to the existing manager instance. Handler registration is published complete only after the set succeeds; partial failure can retry. The general `_lock` remains separate because `register_handler` takes it. Ordinary new managers remain unregistered. |
| Scheduler thread ownership | Seven independent scheduler modules each kept a thread/Event/Lock triple and repeated start/stop/join logic | Each existing module owns one `WorkerLifecycle` instance. No shared worker registry or second scheduler was added. Each module keeps its public start/stop facade and all independent tick, admission, timing, logging, enablement, and worker-name policy. |
| Runtime prewarm | Separate process-owned worker/Event/cache lifecycle | Unchanged and intentionally separate from schedulers. It still signals and joins within the same application shutdown budget. Test isolation now opens a new prewarm epoch only after its prior owner reports idle and its thread handle is no longer alive. |
| Live-probe/read-model cache | Process-local completed cache plus in-flight event/holder records, caller-owned invalidation and TTL | Completed cache ownership, keys, limits, TTL, force-refresh, invalidation and identity fencing are unchanged. Each in-flight holder records the leader thread identity; same-thread same-key reentry now fails fast. `BaseException` outcomes are delivered to attached followers and wake them. |
| Maintenance lease | Fixed host-root lease surrounding the app lifespan | Still owned by `main.py`, outside injectable lifecycle operations, and held around coordinated shutdown. State-root environment values do not redirect the fixed lease. |

## F08 — Worker lifecycle

The shared helper only owns one thread handle, one cooperative `Event`, and one serialization lock per existing scheduler. Scheduler loops do not call the helper; they use the owned event to wait/cancel cooperatively. Holding the lock through `join()` serializes concurrent starts/stops; review confirmed these loops do not acquire the lifecycle lock. A timed-out stop leaves both the live handle and set stop event in place, and start returns `False` until that thread exits. Stop with no handle still returns `False`.

Thread-factory start failures are handled explicitly. If `start()` raises before a worker is live, the unjoinable handle is dropped. If a custom factory raises after creating a live thread, its handle is retained and cooperative stop is requested. Bounded tests exercise both cases, concurrent start calls, timeout/retry, and the seven scheduler facades.

Independent scheduler policies remain separate:

| Scheduler | Preserved policy |
|---|---|
| Maintenance | Immediate first maintenance tick, configured interval, existing warning event and writer admission. |
| Member probe | Immediate bounded member sweep with existing budget/timeout and interval. |
| Active observation | Immediate active-path observation and existing interval/log policy. |
| Subject inventory | Existing optional startup delay, JobManager submission/non-overlap, interval, and logging. |
| External collectors | Existing wait for one full check interval before the first collector pass; same collector selection and error logging. |
| Runtime convergence | Immediate convergence check and separate vpn-auto dispatch, with distinct error logs and existing cadence. |
| Watchdog | Immediate tick, adaptive next interval and existing callback/disabled behavior. |

`main.py` still holds the fixed maintenance lease around the coordinator and all shutdown work. Application shutdown signals every scheduler with zero join budget before spending one shared 14-second deadline across scheduler joins, prewarm, and JobManager drain. Startup database admission remains before scrubbing, extended handlers, schedulers and prewarm. Startup failure still enters the same cleanup path. With startup tasks disabled, schedulers are not stopped by this lifespan, but prewarm and existing jobs are still drained as before.

The Orchestrator independently compared every non-start/stop scheduler function before and after extraction by AST, normalizing only the old stop-event variable to `WorkerLifecycle.stop_event`: **23 functions unchanged, zero unequal**. This is structural evidence that tick bodies, cadence, exception/logging/admission and collector policy remained intact; it is not runtime/performance evidence.

## F09 — Explicit state ownership

The factory is a narrow test seam for the lifecycle coordinator, resolved at lifespan entry so existing `main` monkeypatch seams remain effective. Production uses the same default facades/singletons. The fixed maintenance host lease cannot be substituted through the factory. `create_app()` does not imply independent workers, managers, settings, caches or a second root. The supported API process stays single-process/single-state-root; overlapping production lifespans and live environment-root switching are not promised.

The default `JobManager` remains process-owned and shared by routes, worker jobs and lifecycle drain. Its built-in registration flag now lives on that manager instead of a separate module boolean. The captured manager is deliberately reused for shutdown, preserving one-manager ownership through a lifespan. No route, CLI, API error, or persistence contract changed.

The project map records Core intent in SQLite separately from volatile observation/read-model caches. Cache invalidation remains with existing service mutation owners; native proof is not converted into a cache value and cache TTLs remain observation freshness boundaries. `runtime_prewarm` remains an independent process owner because it has its own admission flag, operation boundary, and restart semantics.

## F10 — Single-flight cache disposition

The Orchestrator's bounded caller audit and characterization are retained outside this source manifest under `/tmp/fwrouter-package-f-audit/`:

- `cache-baseline.json` records source SHA-256 `74d4866f9465df9d72ab3bf4cac63a562dc62fb4ae2f3f3cfb4fdc181f81740a` and controlled baseline observations.
- `characterize_cache.py` loads the actual cache module in disposable workers/threads and bounded all cases. Before the fix, same-key recursion remained blocked; a nonreturning leader left a follower waiting until release; and a custom `BaseException` escaped the leader while a follower returned `None`.
- Call-site review found nested `runtime.summary` projections use distinct keys. StateSnapshot external observations and Xray proof reuse their canonical keys with raw loaders; no built-in same-key recursive loader chain was found. A custom health adapter/raw loader can reenter, so the recursion guard is a fail-fast loader contract protection, not a claim of a demonstrated production deadlock.
- A leader that never returns remains a possible source of unbounded follower wait; the audit did not prove that arbitrary supported loaders always terminate. No generic wait timeout, forced cancellation, stale-value fallback, native freshness extension, or performance claim was added. Loader/caller deadline responsibility remains explicit.

The cache stores the leader's thread id in the existing per-flight holder. A caller joining its own in-flight key raises stable `RuntimeError("LIVE_PROBE_CACHE_REENTRANT_LOAD")`; completed cache hits still return before this check. Different-key nested loads remain supported. Leader `BaseException` is stored like existing `Exception`, re-raised unchanged in the leader, and replayed to attached followers before the event is signaled. In-flight identity checks, clear behavior, normal exception propagation, coalescing, force-refresh, cache bounds/TTL and invalidation owners are unchanged.

## Verification

Baseline before edits at exact `7800ea857d662f718164f0b2a36872678135e235`:

```text
/tmp/fwrouter-private-test-venv/bin/python -m pytest -q \
  backend/tests/test_scheduler_lifecycle.py backend/tests/test_lifespan_shutdown.py \
  backend/tests/test_subject_inventory_scheduler.py backend/tests/test_live_probe_cache.py \
  backend/tests/test_probe_cache_lifecycle.py backend/tests/test_job_manager.py \
  backend/tests/test_main_startup_scrub.py backend/tests/test_maintenance_admission.py \
  backend/tests/test_bootstrap.py
147 passed, 1 existing Starlette/httpx deprecation warning, 14.61s
```

The final focused regression command (ordinary process-safe lifecycle, cache, runtime/read-model, configuration, proof-contract, and Package E schema/admission suites; qualified native/process acceptance excluded) passed **382 tests**, with one existing Starlette/httpx deprecation warning:

```text
/tmp/fwrouter-private-test-venv/bin/python -m pytest -q \
  backend/tests/test_scheduler_lifecycle.py backend/tests/test_lifespan_shutdown.py \
  backend/tests/test_subject_inventory_scheduler.py backend/tests/test_live_probe_cache.py \
  backend/tests/test_probe_cache_lifecycle.py backend/tests/test_job_manager.py \
  backend/tests/test_main_startup_scrub.py backend/tests/test_maintenance_admission.py \
  backend/tests/test_bootstrap.py backend/tests/test_runtime_prewarm.py \
  backend/tests/test_external_collectors.py backend/tests/test_watchdog.py \
  backend/tests/test_configuration_contract.py backend/tests/test_runtime_summary.py \
  backend/tests/test_native_proof.py backend/tests/test_database_schema_lifecycle.py
382 passed, 1 warning, 40.76s
```

Additional checks: `backend/tests/test_offline_maintenance_admission.py` **29 passed**; `tests/acceptance/source_catalog.py` **PASS, 57 statically inventoried nodes**; `tests/gates/test_gate_contract.py tests/gates/test_acceptance_contract.py` **296 passed, 1 skipped, 5 existing unknown-browser-mark warnings**. The existing skip is `test_gate_contract.py:2167`, because this host lacks `/usr/bin/time`, required by the real child runner. Pinned Ruff on all changed Python files and `git diff --check` both **PASS**. No acceptance scenario/catalog definition was modified; the source-derived inventory still matches.

The first broader local composition exposed two test-harness isolation boundaries: the process-execution guard denied `os.fork`, so the recursion regression uses a bounded disposable thread whose test-only Event returns after 0.5s if the guard regresses. The application cache code does not receive this test bound. The first broad run also showed that a prior maintenance lifespan leaves the prewarm stop Event set for later tests. The fixture now reserves the idle admission flag, joins/rechecks a previous worker handle outside its lock, then clears stop only after that worker is dead. This does not clear a live worker's cancellation signal or bypass the production owner. The final focused regression passed with both corrections.

Exact-head GitHub Actions, independent QA and offline artifact verification are **NOT RUN by this writer**; they remain required Package F gates for the Orchestrator. Qualified native/process proof remains reserved for the established hosted L6 profile. No production DB, credentials, native runtime, system service, CI workflow or deployed application runtime was touched.

## Remaining gates and risks

- Package F is not complete until independent architecture/security QA and required exact-head hosted gates pass and their evidence is verified.
- No universal deadline can bound a custom cache loader that never returns. Existing caller/loader deadline ownership is retained; no stale observation substitutes for current state.
- Concurrent app lifespans and environment-based state-root switching during active work remain unsupported; the refactor does not enforce or broaden those semantics.
- No Package G work, PR, merge, deploy, or live verification occurred.

## Independent source QA — frozen candidate

Independent architecture/security/concurrency review **PASSED** against baseline `7800ea857d662f718164f0b2a36872678135e235` and the 31-file manifest SHA-256 `84aae00e1068b6f75882e5f05c2a5114a8942be0bf65db51b971a78521e8b03f`. The reviewer independently recomputed every listed digest and the protected roadmap-pointer digest, inspected all changed production code and relevant callers, maintenance admission/lease placement, scheduler policies, registration locking, cache freshness/invalidation/error propagation, and the disposable prewarm isolation boundary. No material finding was reported.

Independent isolated runs: lifecycle/scheduler/manager/cache **62 passed**; startup/maintenance admission/prewarm **59 passed**, each with the existing Starlette/httpx deprecation warning. Diff whitespace check **PASS**. The Orchestrator additionally verified the unchanged 212-file gate catalog, all candidate digests, and `installer/check-clean-tree-surface.sh` **PASS**. New helper modules import only standard-library dependencies; no reverse service ownership edge was introduced. The scheduler policy AST check covers 23 unchanged functions in all seven modules after only stop-Event reference normalization.

These source/local checks do not establish hosted acceptance. Commit/push, exact-HEAD push-fast and qualified branch L6, offline evidence verification, and the canonical closure remain pending. No PR, merge, deployment, production operation or Package G work is authorized by this checkpoint.

## Verified implementation acceptance — 2026-11-03

Implementation commit `35ed543295b91363b06a6c69593c2774b7d3f60a` was committed and pushed normally on `stage6/backend-completion`, directly after baseline `7800ea857d662f718164f0b2a36872678135e235`. No amend, force-push, fetch, reset, clean, PR, merge or deployment occurred. The 31-file source manifest remains unchanged from independent source QA. Historical pending statements above describe their original checkpoints and are superseded by these verified results within this scope.

| Gate | Actual evidence | Result |
|---|---|---|
| Source architecture/security QA | Independently recomputed 31 digests; lifecycle/worker/manager/cache callers and invariants reviewed | PASSED; no material finding |
| Local regression | 382 focused + 29 offline-admission tests | PASS |
| Independent local checks | 62 lifecycle/scheduler/manager/cache + 59 startup/admission/prewarm tests | PASS |
| Catalog / gate contracts | 212-file manifest; 57 acceptance scenario nodes; 296 gate/acceptance tests | PASS; one explicitly recorded local `/usr/bin/time` capability SKIP |
| Push-fast | [41635259711](https://github.com/Coxf0rd/fwrouter/actions/runs/41635259711), exact implementation SHA, event `push` | SUCCESS |
| Complete qualified branch L6 | [41635279732](https://github.com/Coxf0rd/fwrouter/actions/runs/41635279732), attempt 1, exact implementation SHA | SUCCESS, 19/19 jobs |
| Required test inventory | Immutable plan, manifest `2026.11.03.2` | 211 selected safe files of 212 catalog files; zero selected files NOT RUN |
| Result coverage | Independently checked `reports/visibility.json` | 3,463 unique IDs, 3,509 executions/PASS; zero FAIL/SKIP/BLOCKED/NOT RUN |
| L5 contract coverage | Ten required anchors | PASS |
| Package E current-schema Compose preservation | Actual `test_current_schema_initialization_preserves_control_plane_and_native_state` in both hosted repetitions | PASS in both |
| Final L6 artifact | `13230876343`, `fwrouter-l6-evidence-35ed543295b91363b06a6c69593c2774b7d3f60a-41635279732-1` | Unexpired; downloaded ZIP digest equals GitHub API digest |
| Independent offline verification | Orchestrator and separate QA replayed the existing verifier from clean exact-source checkout | `l6_evidence_verified_not_deploy_authorized` |

Artifact SHA-256: `d24ce239f386eeb469e107c0ea7af501869b24e24d55b23c2f61ea6c127e8ffd`. Evidence and independent metadata snapshots are retained locally under `/tmp/fwrouter-package-f-evidence-41635279732/`; clean source is `/tmp/fwrouter-package-f-offline-35ed543`. The separate reviewer re-established workflow/run/attempt, source SHA, event/ref, repository, all 19 job results and artifact metadata with read-only GitHub API calls, recomputed the ZIP digest, and replayed the official verifier independently. The verifier validates embedded source, plan, complete content inventory, required test results and producer evidence. GitHub artifact retention is finite; this receipt does not claim permanent archival.

Qualification scope is `branch-regression-non-promotional`, `minimum_repeats=2`, `deploy_eligibility=false`. Qualified native, packet, provider, browser and recovery evidence retains each existing profile's boundaries. The sole omitted catalog file is the established optional/deferred `tests/application_acceptance/test_xray_worker_crash_l7.py`; it was not executed and is not claimed PASS. No CI policy, inventory exclusion, budget, timeout or security/native assertion was weakened. Stock Docker/host-systemd/reboot parity, production traffic and external-client live acceptance are not inferred.

Residuals remain explicit: a custom loader that never returns can still hold followers; cache TTL does not supply an I/O deadline. No built-in recursive chain was found, while adapter/loader reentry now fails fast. Concurrent app lifespans and active state-root environment switching remain unsupported. These are documented compatibility/availability boundaries, not hidden PASS exemptions.

This report-only closure commit does not change the reviewed 31-file source manifest. Its own exact-HEAD push-fast/L6 and independently verified bundle must pass before the canonical roadmap records overall Package F COMPLETE. That final receipt stays outside the Git tree to avoid a self-referential source SHA. Package G remains NOT STARTED; continue future work on the same stage branch.
