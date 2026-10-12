# Selective component stability — implementation contract

Status: PLAN ONLY; no implementation or new test execution.
Reviewed source: 8ff2648 (documentation), application/test checkpoint ec8455c.
Evidence: [SELECTIVE_DIAGNOSTIC_38300332724.json](SELECTIVE_DIAGNOSTIC_38300332724.json), [REPORT.md](REPORT.md).

## Goal and attribution boundary

Remove unowned background work from ordinary component fixtures and fix the independently source-confirmed stale cache publication defect. Keep routing assertions, native proof, Core ownership/fencing and provider semantics unchanged. Failed hosted repetition4 consumed HTTP/degraded health instead of its fake; it correctly rendered fail-closed DROP. The exact prior thread was not recorded; deterministic interleaving proof precedes claiming definitive causation.

The prewarm dispatch policy is tested with spies, but its actual worker lifecycle has no dedicated test. Successful APPLY starts it incidentally. JobManager idle does not mean prewarm idle. Merely patching another adapter alias or increasing timing margins is insufficient.

## Package A — test ownership, no production prewarm redesign

Files: backend/tests/conftest.py, backend/tests/test_apply_pipeline.py, new backend/tests/test_runtime_prewarm.py, test manifest/contracts as needed.

1. Centrally prevent incidental async prewarm in ordinary unit/component fixtures. Use the existing prewarm admission seam under explicit test ownership; do not add a production environment switch or wait-idle API solely for pytest. The existing shared admission flag can block ordinary scheduling across captured aliases, provided setup first asserts there is no already-running worker. This is a documented component boundary, not prewarm acceptance.
2. Preserve existing dispatch spies/assertions (apply selective-default, startup, UI changed/no-op). Avoid globally inventing healthy runtime or patching all health consumers.
3. Dedicated lifecycle tests explicitly opt into real Python threads with event barriers. Track and bounded-join all owned threads before adapter restoration/cache clearing; assert no survivor. They must test one admitted worker, rejection of a concurrent request, completion/exception releasing admission, and warm composition invoking the expected read models once. Explicit opt-in must not silently execute native/network work.
4. Preserve exact IP/domain steering, TCP mark, fail-closed and post-apply assertions. Keep the failure diagnostics. Verify current fake health is used for candidate generation, not merely that the final API state is successful.
5. Default isolation affects backend component fixtures only. Application/native/browser Compose servers retain their real scheduling and proofs. Verify qualified-profile boundaries explicitly; do not blanket-disable prewarm in application configuration.

## Package B — minimal cache invalidation fence

Files: backend/fwrouter_api/services/live_probe_cache.py and backend/tests/test_live_probe_cache.py.

Use the identity of the existing in-flight event/holder entry as an ownership token. No persistent revision, global epoch or growing per-key generation map is necessary.

- A leader publishes into cache only while its entry is still the current entry for that key.
- It removes in-flight bookkeeping only if it still owns that entry. It always completes its own event/holder, including errors.
- Global clear invalidates all entries. Matching clear invalidates both cached and in-flight matching keys, including loads not yet cached; unrelated keys remain intact.
- Calls already attached before invalidation finish with their original observation/error. Invalidation does not cancel their network operation. Calls begun after invalidation cannot join the invalidated entry or reuse its late result.
- Preserve TTL-after-loader, reuse/single-flight, force-refresh and error propagation. No automatic retry, extra probe, lock held across loader/wait, sleep or routing change.
- Correcting the connection-to-global-external-VPN cache-key mismatch is a separate finding, not required to alter routing/provider lifecycle in this package. Record it without claiming it fixed.

## Deterministic acceptance before repetitions

Use Event barriers and bounded finally cleanup, never scheduling sleeps. Prove the regression detects the old implementation in an isolated module copy, then prove the current implementation passes. Do not alter the worktree/production or create another branch for baseline comparison.

Required scenarios:

| Contract | PASS criterion |
|---|---|
| Old loader -> clear -> new same-key loader | Old completion cannot publish over/remove replacement entry |
| Pre-clear waiter vs post-clear caller | Prior cohort completes; new cohort receives fresh result |
| Matching invalidation | Matching pending work fenced; unrelated key preserved |
| Loader exception | Waiters receive error; no stranded event; fresh later load possible |
| TTL/force-refresh/single-flight | Existing semantics and bounded call counts preserved |
| Prewarm admission/lifecycle | Single owned worker; busy rejection; success/error cleanup |
| Component boundary | Ordinary apply launches no unowned warm work; intended dispatch assertions remain |
| Selective generation | Ready fake supplies candidate contour/ports; exact steering and mark assertions pass |

## Efficient hosted execution order

1. Implement both small changes and their tests locally; perform source-only AST/diff/manifest checks on minisk. No local application tests or native processes.
2. First hosted checkpoint: new cache/lifecycle/fixture contracts, existing dispatch contracts, both selective cases and affected component suites. Run the intermittent cohort up to5 independent successful repetitions, stop on first failure with receipts. If deterministic proof fails, do not advance to broad profiles.
3. After targeted PASS, run one final policy-selected affected L0-L5 workflow, including required source-matched native/application profiles. Cache has consumers in Health, routing, state, Xray, DNS, runtime and registry; do not narrow mapping to suppress legitimate gates. This is not L6. If the configured gate requires green-only profile repetition, retain it in that one workflow.
4. Review source/Tests/Commit evidence and sync report/roadmap. No production deploy, PR, merge, Phase E/CD or automatic L7. Push only the authorized CI branch; do not cancel an active run with another push.

## Why this is the smallest sound plan

- Fixture-only suppression fixes incidental work but leaves a genuine cache invalidation defect; source-only fencing cannot stop new post-clear background work using the wrong test adapter alias. Both boundaries are needed.
- Token identity reuses current synchronization; an epoch map or new scheduler is unnecessary.
- Controlled lifecycle coverage prevents isolation from masking the component being suppressed.
- Patching every adapter alias is brittle and hides shared-cache ownership; increasing TTL/deadlines or dropping route assertions changes the wrong contract.
- No new production wait/join API, changes to runtime scheduling, images, namespaces, selectors, provider PATCH or firewall are proposed.

## Done and stop conditions

PASS requires deterministic race proof, zero leaked test threads, all targeted repetitions passing, required affected gates actually executed, and honest source-matched receipts. Existing full native PASS is historical evidence, not substituted for tests of changed cache source.

Any new failure must name its predicate and cohort before another run. Do not turn the five repetitions into retries until green. Final Phase D verdict is decided only after applicable gates; unresolved mandatory failures remain BLOCKED.

## Time budget

Implementation/review: approximately45-75 minutes. Targeted hosted execution: approximately5-15 minutes excluding GitHub queue. Final affected/native gate: approximately20-35 minutes if selected. Evidence/docs:10-15 minutes. Total planning range: roughly1.5-2.5 hours if no new root cause emerges; this is not a guarantee or a claim of completed work.
