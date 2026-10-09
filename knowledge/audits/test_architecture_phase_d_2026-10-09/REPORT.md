# Phase D — GitHub Actions CI

Status: IN PROGRESS / acceptance still BLOCKED; resumed for targeted diagnosis of the existing infrastructure failures. Neither infrastructure acceptance nor Phase E is claimed.
Source checkpoint: `6bdf37e`; branch `stage/test-architecture-ci-stabilization`.
All executed application tests run on disposable GitHub-hosted Ubuntu 24.04.
Local checks are source-only syntax/diff/YAML parsing. No production tests,
deploy, restart, provider mutation, PR, merge or Operational branch changes.

## Initial remote evidence

| Run | Commit | Actual result | Interpretation |
|---|---|---|---|
| [37817929947](https://github.com/Coxf0rd/fwrouter/actions/runs/37817929947) | `c8914b3` | FAILED | First remote branch push supplied all-zero `before`; fail-closed base check rejected it. Historical pytest hit an out-of-owned-root JUnit write; no valid exact-node report. |
| [37818159278](https://github.com/Coxf0rd/fwrouter/actions/runs/37818159278) | `3de4b63` | FAILED | Real isolation smoke PASS; fast gate refuses an unregistered dispatcher; historical seven 0 PASS / 7 FAIL / 0 SKIP; native attempt rejected Docker mount representation before starting tests. |

The second run successfully downloaded and hash-verified official pinned Xray
26.2.6, Mihomo 1.19.31 and complete Playwright 1.55.0 Chromium 1187.
Both functional attempts were NOT RUN, not native acceptance: confinement
inspection rejected unexpected mounts. Owned Docker resources were removed.

## Historical failure attribution

Five exact read-only contract tests show an added `watchdog_state` row. Source
trace is `load_watchdog_runtime_state` → `ensure_watchdog_runtime_state_row` →
`INSERT OR IGNORE`. This is a reproducible application read-path defect, not a
fixture excuse. A narrow source correction may remove initialization only from
read; writers retain row initialization and empty state projection remains.

Topology reports single observations `Single`, `Unavailable` where the old
performance test expects none. Current singleton/failure fallbacks must be
reviewed against supported runtime capabilities; assertions are not weakened.

The Xray lifecycle fixture replaces `reconcile_xray_subscription_profile_nodes`
(the implementation reached by its wrapper) with a synthetic result omitting
`deleted_count`. Its KeyError is an overmocked fixture issue. Native application
CRUD is a separate requirement and cannot silently retire this exact test.

## Environment and resource attribution

Actual runner: 4 CPUs, 15 GiB RAM, Docker 28.0.4, Compose 2.38.2. Observed disk
was 145 GiB with 86 GiB free; do not assume this exceeds documented guaranteed
runner capacity for every job. Container envelope remains 2 CPU / 2 GiB /
256 PIDs / 512 MiB tmpfs, no published ports or Docker socket.

Initial native outer command: 40.21 s, max RSS 78,148 KiB, user/system CPU
3.65/1.99 s; these are launcher/build-client metrics, **not** application/container
peak resource metrics. Actual tests had not started. Raw resource records and
receipts are under `evidence/run_37818159278_native/`.

## Evidence policy

JUnit/inner receipts must match exact catalog IDs and source/profile/plan.
Skipped, incomplete, wrong-source or cleanup-failed results cannot establish
acceptance. Functional 37 does not substitute the six legacy qualified-process
suites: protocol-native, traffic script, two flock paths, archive subprocess,
and Docker Xray readback remain individually required when selected.

Host systemd/nftables/reboot and production stock startup paths are explicitly
unverified; no unsafe hosted privilege is introduced to simulate them.

## References

- [Hosted runner documentation](https://docs.github.com/en/actions/reference/runners/github-hosted-runners).
- [Manual workflow default-branch restriction](https://docs.github.com/en/actions/how-tos/manage-workflow-runs/manually-run-a-workflow).
- Phase C source catalog and traceability remain in the sibling Phase C report.

## Read-path correction verified remotely

`7771690` changes one application line: read no longer initializes watchdog row.
Writers retain initialization. New regression checks zero INSERT/UPDATE on
empty/subsequent reads and positive writer initialization; SQLite teardown is
explicit. Source was pushed with CI checkpoint `0672587`, no deploy.

Run [37819443588](https://github.com/Coxf0rd/fwrouter/actions/runs/37819443588)
exact historical cohort is **5 PASS / 2 FAIL / 0 SKIP**, complete exact-node
coverage and owned cleanup. The five read-only assertions are unchanged.
Topology and the overmocked Xray fixture remain red, not allowlisted.

## Local-check deviation

One implementation engineer mistakenly executed the 20-case stdlib-only
`tests/gates/test_acceptance_contract.py` locally. It imported test-infrastructure
modules only and did not access application/runtime/production capabilities.
Its result is excluded from hosted acceptance evidence. Further local tests
were stopped; all application/native/browser/baseline suites run remotely.

## Repository policy observation

Read-only GitHub API inspection: active ruleset `Main` (`24678746`) requires PR
and disallows non-fast-forward updates. It currently has **no required-status-
check rule**. Workflow failure is not yet enforced as a repository merge gate.
Classic branch-protection endpoint returns 404 because this repository uses
rulesets. No repository protection settings were changed. Configuring verified
check contexts is an explicit Phase E/open acceptance item; no PR/merge here.

## Wider gate findings

At `0672587`, broad affected L1 recorded 685 exact nodes, 59 failures novel to
the durable registry plus one unapproved registry baseline failure. This does
not establish 59 new product regressions: the earlier bounded historical
cohorts did not execute these nodes. The initial ledger retains each ID and
marks attribution UNDETERMINED pending diagnostic/source evidence.

`ffaa994` was pushed with a syntax error in the concurrently edited aggregator
because root's shell did not stop after AST failure. This checkpoint is invalid
and not acceptance evidence; it is retained for history. Subsequent integration
uses separately completed owner checkpoints plus fail-fast source checks before
commit/push. Its native preflight passed, but application source-manifest guard
rejected mismatched hash roots before scenarios; 37 cases NOT RUN.

## Pause checkpoint — current check completed

Latest pushed source: `3a4209b`. Run [37822381741](https://github.com/Coxf0rd/fwrouter/actions/runs/37822381741) is COMPLETED / FAILURE; no further runs or pushes were started after the pause instruction.

- Confinement and pinned runtime provisioning passed. The actual 37-case functional application/native/browser cohort executed twice; each raw pytest summary is **6 passed, 31 failed, 11 deselected**. The 11 are the explicitly excluded L7 cohort, not skipped functional tests. Each pytest pass took approximately 167 s. These are failed execution observations, not accepted native proof.
- Many failures cascade through actual Xray subprocess return code `XRAY_NATIVE_EXIT_23` and subsequent local provider verification failure. The exact native stderr/candidate is missing from retained diagnostics; whether validation or API readback caused that exit remains UNPROVEN. Do not classify this as a production bug without that evidence.
- Additional independent source/test mismatch: unknown-source provider request returns `PROVIDER_DISABLED` where the test expects `SUBSCRIPTION_SOURCE_NOT_FOUND`. No assertion was weakened.
- JUnit/inner receipt retrieval failed after pytest, so there is no valid complete native acceptance receipt. The exact docker-cp failure is confirmed; its underlying cause is not established.
- QCP is NOT RUN because its version guard expects a different Mihomo version representation from the verified `1.19.31` input. Docker-Xray is NOT RUN because the daemon architecture guard expects `x86_64` while actual Docker reports canonical `amd64`. These are harness admission mismatches, not native PASS/FAIL.
- Historical seven: **5 passed / 2 failed / 0 skipped**. Five read-only cases pass after the separately recorded `7771690` source correction. Topology and the overmocked Xray fixture remain failed in this executed checkpoint; their proposed fixture corrections are uncommitted and NOT RUN.
- Routine fast/affected gate fails the output-cap contract: a requested 128-byte cap retained 1184 bytes. A bounded-output correction is in the working tree, uncommitted and not remotely verified.
- Resource records: outer functional commands approximately 214/194 s, host process max RSS approximately 76/73 MiB. These are not container peak memory. Runner 4 vCPU / 15 GiB; about 5 GiB disk growth during builds; cleanup evidence shows no retained owned containers/networks. Outer filesystem output includes image/build work and is not application write attribution.

The working tree deliberately retains unfinished gate/fixture edits and audit evidence. No new commit is made while this checkpoint is still unverified. The temporary validation workflow remains in source for controlled continuation; removing it and restoring the normal push trigger is still a delivery gate. L7, permanent event-trigger acceptance, native receipt completeness and affected gate PASS remain OPEN. Operational branch and production runtime were not changed.

Next bounded investigation, only after resumption: retain exact native stderr/candidate diagnostics, identify exit 23, correct proven profile-format mismatches, then rerun only the required affected gates. Do not restart broad CI blindly.

## Resumed targeted diagnosis

Existing uncommitted fixture/gate/workflow changes were retained. The 31 raw failures span 15 Core/provider/Mihomo cases, eight Xray generation cases, three Xray API cases, three browser cases and two SQLite cases. These counts describe failed test observations, not 31 independent application defects. Repeated Xray CLI exit 23 and downstream local verification failures suggest shared prerequisites; exact attribution requires preserved native stderr. One independent unknown-source provider error-code discrepancy remains open.

Preflight source confirms two representation bugs: qualified-child requires `Mihomo Meta v1.19.31` although provision exports `1.19.31`; Docker-Xray expects `linux|x86_64` although the actual qualified daemon reports `linux|amd64`. Accept equivalent version/architecture representations only while retaining pinned binary hashes and observed runtime/image platform checks.

Prior pytest logs confirm JUnit generation at the expected container path; Docker archive-copy failed with its stderr suppressed. Container deletion as the cause is not established. Preserve exact transfer diagnostics before assigning root cause.

Remote sequence is explicitly stage-gated: focused static/bootstrap/isolation → one real application Xray create/delete scenario → qualified native cohorts → affected application/browser and the 37-case cohort only after common blockers are removed. No automatic L7. Diagnostic subset evidence cannot promote the complete functional suite.

## Final bounded checkpoint — 2026-10-09

**Phase D: BLOCKED.** Production runtime was not changed. No Phase E/CD, PR, merge or L7 was started.

### Proven corrections and remote evidence

- `65a0fa6`: bounded/redacted native stdout/stderr, exit codes, worker/Compose state and unconditional artifacts; genuine JUnit retrieval fallback plus explicit error XML when unavailable. Version/architecture guards accept verified equivalent representations without relaxing pins or confinement.
- [37895517437](https://github.com/Coxf0rd/fwrouter/actions/runs/37895517437): static/bootstrap/isolation/focused infrastructure checks PASS; historical 6 PASS / 1 FAIL; singleton application native test 0 PASS / 1 FAIL. Xray stdout establishes exit 23: CLI cannot determine format of `config.json.candidate`. Production adapter uses a `.json` candidate path; this is a harness mismatch, not established production failure. Docker archive-copy cannot find the report, while bounded container exec retrieves the real XML; Docker internals remain unexplained.
- `50e8a9e`: validate byte-identical private `.json` candidate with hash, ownership, size and cleanup checks. Topology fixture checks exact batch membership without unsupported SQL row-order assumptions.
- [37895910112](https://github.com/Coxf0rd/fwrouter/actions/runs/37895910112): historical **7 PASS / 0 FAIL / 0 SKIP**, exact-node coverage and cleanup verified. Real Xray candidate validation and native loaded-client readback succeed. Singleton application test remains 0 PASS / 1 FAIL at materialization; qualified/full gates SKIPPED, not PASS.
- `d6db5c2`: preserve complete failed-job diagnostics without changing the status assertion.
- [37896406104](https://github.com/Coxf0rd/fwrouter/actions/runs/37896406104): historical **7 PASS / 0 FAIL / 0 SKIP** again; static/preflight PASS; singleton application test **0 PASS / 1 FAIL**. Exact nested failure is `MIHOMO_GENERATION_STALE_BEFORE_PUBLICATION`, stage `generation_publication_revalidation`. Candidate generation/native validation, native restart/readiness, selector restore and promotion report success; final publication ownership check refuses. Recovery reports `verification_callback_not_requested`. The precise revision/incarnation/fingerprint/config comparator is not yet established. Do not bypass it or claim convergence.

### Local-only checkpoint and external blocker

`02e75d0` adds an acceptance-worker-only observer calling the original fingerprint and fence unchanged. It records bounded hash-only expected/observed values and differing fingerprint leaf paths on refusal. AST and diff whitespace checks PASS; **remote execution NOT RUN**.

Push of `02e75d0` was rejected with `ERROR: You must verify your email address. See https://github.com/settings/emails.` Last remotely pushed checkpoint remains `d6db5c2`. No authentication/protection workaround was attempted. Email verification and a permitted branch push are required before the one next diagnostic run.

### Remaining gates

- Exact publication-fence refusal cause and genuine application CRUD convergence remain open; fixture-vs-product attribution is unknown.
- Qualified native cohorts: admission corrections implemented but actual cohorts NOT RUN in this resumed sequence because the minimal prerequisite failed.
- Full 37-case application/browser cohort NOT rerun after the fixes; historical 6 PASS / 31 FAIL is retained, not upgraded. Failures are grouped by common prerequisites, not called 31 regressions.
- Independent unknown-source error-code discrepancy and wider affected-suite attribution remain open.
- Permanent push/PR/main event acceptance, temporary validation trigger removal, full receipt completeness, mandatory affected gates and Phase E acceptance remain open.
- L6 and L7 NOT RUN. Systemd/nftables/host recovery capability is not established by native process tests.
- Seven historical tests now pass remotely, including five read-path tests after the previously documented source-only correction, topology and the component Xray fixture. Component fixture PASS is not native application acceptance.

All original uncommitted changes were retained and incorporated into local checkpoints. Operational Performance Fixes branch and production were untouched. Resume with the single fence diagnostic after access is restored; only then qualify native gates and full functional coverage.

## Resumption after email verification

Authorized local checkpoints were pushed successfully. Run [37904598829](https://github.com/Coxf0rd/fwrouter/actions/runs/37904598829) gives exact refusal evidence in `MIHOMO_FENCE_EVIDENCE.json`: writer guard, revision=1, incarnation, active-config hash and verification all match; only input fingerprint changed because `tables.routing_global_state` changed from absent to the canonical default singleton. Acceptance disabled startup tasks but omitted startup intent initialization. `b633cc8` initializes through the existing bootstrap intent reader before native generation and checks repeat-read stability. No fence was relaxed.

The observer initially caused a harness circular import before API readiness (run37904156917); `650f342` installs it after the established runtime import/binding order and adds an AST regression. Run37905613570 then passed both create jobs but exposed diagnostic redaction incorrectly applied to internal native RPC identities. `8c2cbc6` separates bounded raw private RPC output from redacted retained artifacts. Its new fake-runner fixture missed `root`; run37906948422 failed infrastructure L1 and native was SKIPPED; `93445c4` completes that fixture.

Run [37907214978](https://github.com/Coxf0rd/fwrouter/actions/runs/37907214978) reaches successful create/update/worker restart/parity/delete jobs; final native helper rejects successful `{}` empty-user readback. Pinned [Xray v26.2.6 schema](https://github.com/XTLS/Xray-core/blob/v26.2.6/app/proxyman/command/command.proto) defines a repeated users field, omitted when empty. The application adapter has the same parsing incompatibility. Separate source correction `0bad779` accepts exactly `{}` and retains malformed/error/identity checks, with unit negatives and native zero-client regression. This source correction is NOT deployed. Harness correction is `bca9517`.

Run [37908331340](https://github.com/Coxf0rd/fwrouter/actions/runs/37908331340) passes static/bootstrap/isolation/focused L1, historical **7 PASS / 0 FAIL / 0 SKIP**, and the actual singleton application native CRUD diagnostic **PASS**. Qualified and full functional jobs are intentionally SKIPPED for the minimal stage, not acceptance PASS. Proceed to qualified lanes only; L7 remains NOT RUN. Overall Phase D still BLOCKED pending mandatory qualified/affected/application/browser gates.

## Qualified native acceptance checkpoint

Run [37912831833](https://github.com/Coxf0rd/fwrouter/actions/runs/37912831833), source `d6dc6ea`, confirms exact QCP cohort **26 PASS / 0 FAIL / 0 SKIP** in 3.274 s and isolated Docker Xray **1 PASS / 0 FAIL / 0 SKIP** in 2.321 s, with owned resources removed. The minimal application native CRUD repeats PASS. Full functional job was intentionally SKIPPED for this qualified stage.

Additional infrastructure corrections were evidence-led:
- `435d0e5`: retain bounded preflight/native/worker diagnostics and fixed-path JUnit fallback; put QCP JUnit under the existing coordinator-owned root. Xray temp fixture uses owned pytest `tmp_path`.
- `53c0a38`: explicit bounded executable QCP tmpfs, with actual mount flags retained; normalize exact known JUnit package prefixes. Archive copy by immutable ID is admitted only after fresh bounded inspect of this run's reserved owned-container name returns that exact ID; foreign IDs and paths remain rejected.
- `9d5a7f8`: QCP pytest workdir `/workspace/backend`; successful functional exit accounting begins at zero and preserves actual failures.
- `02b1857`: context exporter preserves source executable bits as sanitized 0755/0644, never special bits; symlinks stay rejected. Git's 100755 collector was previously exported 0644.
- `d6dc6ea`: provide real `jq` only in the acceptance image and record its container version, replacing no mocks or collector assertions.

The earlier QCP tmpfs assertion established a mount-admission mismatch, but old exact flags were not recorded; do not retrospectively claim a specific old flag. Current observed flags are rw/nosuid/nodev/relatime, type tmpfs, with no noexec, and configured bounded exec tmpfs.

Mandatory full affected/application/browser gates remain pending. L6/L7 and production deployment remain NOT RUN. Proceed to the controlled functional stage; no blanket baseline exceptions are authorized.

## First full functional run after native qualification

Run [37926035895](https://github.com/Coxf0rd/fwrouter/actions/runs/37926035895), source `81f54f7`, confirms prerequisite minimal application CRUD and both qualified native gates PASS. The full application/browser suite collected **37: 15 PASS / 22 FAIL / 0 SKIP**; it was not repeated after failure. See `FUNCTIONAL_CHECKPOINT_37926035895.json` for exact IDs and outcomes. Receipt: wall 275.539 s, CPU user 62.111 s / system 11.15 s, maximum-process RSS high-water mark 192244 KiB (not aggregate container memory), owned temporary files 21080697 bytes, cleanup errors empty.

The routine affected gate did not execute: its plan contained L7 and the existing admission correctly rejected `plan requests a forbidden level`. Root cause is `make_plan(include_native=True)` treating all opt-in domain suites as native, including the separate L7 worker-crash suite. The correction keeps L7/destructive suites outside ordinary execution, records exact deferred suites and `NOTRUN_RELEASE_GATE`, and preserves fail-closed plan validation. L7 is not an automatic requirement on every broad-domain commit, and no L7 acceptance is claimed.

Provider failures have not yet been attributed to a particular exception. Seven assertion payloads explicitly report `PROVIDER_LOCAL_VERIFICATION_FAILED`; runtime verification catches exceptions under that code and existing worker stderr tails were empty. A qualified-worker-only phase observer records bounded redacted exception type/message and re-raises unchanged. A fixed single-node provider diagnostic is being used before any further complete functional run. The other setup/barrier failures cannot yet be conclusively attributed to this same cause.

Two Xray negative tests require `ok=true` even when an injected failure has already completed before the API response; the API deliberately returns terminal failure as `ok=false`. Both native fault injections were proven in diagnostics. Their assertions now require the returned job identity and retain terminal-failure/native-state checks. Browser Xray failure acceptance similarly waits for terminal job state; cleanup no longer closes the browser after its Playwright event loop has exited. Provider controls browser setup now selects the actual Controls tab. The unknown-source configs endpoint discrepancy remains separately unresolved, with its expected not-found assertion retained.

Public artifact redaction masks UUID/email failure text while preserving exact JUnit testcase identities; private native RPC proof remains unmodified. These source changes require remote verification. Phase D remains **BLOCKED**, no application/native tests were run on production, no production source was deployed, and no PR/merge/CD/Phase E was started.

## Provider diagnostic attribution

Runs [37928385401](https://github.com/Coxf0rd/fwrouter/actions/runs/37928385401) and [37929010066](https://github.com/Coxf0rd/fwrouter/actions/runs/37929010066) execute only one registered provider application scenario after passing static/bootstrap/L1. Both reproduce FAIL. The second proves the observer is installed but the verifier is never entered: the generic code is also assigned by the provider operation to an unverified refresh result, so it did not prove a swallowed verifier exception.

Exact source attribution: `subscription_pipeline._validate_generated_candidate` calls its native validator without a path; the production validator resolves the canonical `MIHOMO_CANDIDATE_CONFIG_PATH`. The qualified worker replacement instead sent an empty path to `mihomo_test_config`. The native RPC resolves that path outside the owned test state and rejects it before native validation or provider verification. The correction restores omitted-path handling through the generator's existing state-aware candidate-path resolver in the test transport and retains the owned-path guard. No production selector, provider policy, mutation, fence or validator was changed. A pure gate contract covers omitted-path resolution and foreign-path rejection. The next single-provider hosted run must validate this correction; full functional acceptance is still pending.

The native pipeline validator has a literal production default constant, whereas candidate generation uses `_resolved_candidate_config_path()` under `FWROUTER_STATE_DIR`. The qualified transport deliberately resolves the generated owned path using that existing resolver and rejects mismatch; it does not mutate production constants. This is a source-identified isolated-state compatibility boundary, not proof of a production routing defect.

## Provider diagnostic clarification — 2026-10-09

Runs [37929892962](https://github.com/Coxf0rd/fwrouter/actions/runs/37929892962), [37930502888](https://github.com/Coxf0rd/fwrouter/actions/runs/37930502888), [37930988561](https://github.com/Coxf0rd/fwrouter/actions/runs/37930988561), and [37931693745](https://github.com/Coxf0rd/fwrouter/actions/runs/37931693745) each execute the single registered provider diagnostic and each finish **1 FAIL / 0 PASS**. Source validation is **PASS** after `3c43390`, but the provider operation still reports `PROVIDER_LOCAL_VERIFICATION_FAILED`.

The recorded generation callback result is `provider_target_unconfirmed` with `ok=false`; the callback returned a negative result and the evidence does **not** show that it raised an exception. Core selection reports `selection_outcome=not_applied`; its on-demand check examined one member and failed it (`checked_count=1`, `failed_count=1`). In run 37931693745, the native group-delay probe for the owned loopback bridge returned HTTP 504. These observations do not yet establish why the probe timed out or why the provider target remained unconfirmed. The precise root cause remains **UNKNOWN**; do not attribute this to a swallowed callback exception.

Commit `f79e78c` adds a bounded, redacted observer and allows at most one staging-member diagnostic. Run [37932841326](https://github.com/Coxf0rd/fwrouter/actions/runs/37932841326) was still running when this clarification was recorded. Its result is pending; it is not a PASS claim. Full functional acceptance remains **NOT RUN after these diagnostics**, and provider/native convergence remains unconfirmed.

### Follow-up: staging-member result and unknown-source contract — 2026-10-09

Run [37932841326](https://github.com/Coxf0rd/fwrouter/actions/runs/37932841326) completed **0 PASS / 1 FAIL**. The bridge recorded zero request/response events; the observed upstream summary and client identity were present. The group result was “all proxies timeout”; the individual native result was HTTP 503 with the pinned handler's generic “An error occurred in the delay test” body. That body does not identify the underlying per-member failure, so the actual cause remains **UNKNOWN**. Do not attribute it to SO_MARK: the pinned loopback probe skips mark application. The observer also has a confirmed capture gap: `BufferedReader.read(8192)` can leave live output unavailable until EOF or a full block. The approved `read(1)` observer correction addresses evidence capture only; it does not establish a runtime root cause.

The full 37-case failure also exposes an independent, genuine endpoint-contract discrepancy for a syntactically valid but unsaved source. `POST /subscription/sources/{source_ref}/provider/configs` routes directly to `discover_provider_configs`; that service checks `binding_for(source_ref)` and returns `PROVIDER_DISABLED` for a missing binding before resolving whether the source exists. By contrast, the canonical provider-operation route first calls `_subscription_url_for_source_ref` and returns `SUBSCRIPTION_SOURCE_NOT_FOUND` when the source is not owned by saved subscription state. The configuration-write route likewise validates the source through `save_provider_configuration`, which resolves the saved source before writing. The acceptance test uses `src:` plus 64 zeroes and expects the same canonical not-found result; observed discovery returned `PROVIDER_DISABLED`, so this expectation follows the existing source-ownership contract rather than inventing a new one. No provider call was reached before this failure.

Minimal regression recommendation: add a route/service boundary test with a valid saved ordinary subscription source and a different syntactically valid unsaved ref. Assert unknown discovery returns `SUBSCRIPTION_SOURCE_NOT_FOUND`, known-but-unconfigured/disabled source retains `PROVIDER_DISABLED`, and a provider adapter that fails if called remains untouched in both cases. Keep the existing acceptance assertion; do not weaken it. No application code or tests were changed in this follow-up, and no tests were run locally.

### Provider upstream-routing diagnostic — 2026-10-09

Run [37933351633](https://github.com/Coxf0rd/fwrouter/actions/runs/37933351633) records the generated upstream route changing from `>> direct` before provider generation to `>> fwrouter-api` afterward; the owned bridge recorded zero requests. Source review found that the first outbound generated configuration did not contain an explicit fixture routing rule. This is a plausible fixture-path explanation, not proof that a catch-all or other routing rule caused the failed provider verification.

Commit `45717dc` adds an explicit fixture rule sending the test upstream through `direct`, preserving it through production generation, and a real HTTP 204 assertion. Run 37933351633 does not validate that correction; its remote result is still **PENDING**. The separate source correction for unknown-source config discovery is committed as `7bde084` and remains **NOT DEPLOYED**.

The native Mihomo output also includes warnings about unsupported `tproxy` and binding a nonlocal handoff address (`172.18.0.1`). These are separate host restrictions; current evidence does not show that the group-delay path requires either capability. No full-host parity or successful provider/native convergence is claimed.

### Provider diagnostic follow-up — run 37933861681

Run [37933861681](https://github.com/Coxf0rd/fwrouter/actions/runs/37933861681), head `45717dc`, completed **0 PASS / 1 FAIL / 0 SKIP** after hosted runtime preflight, confinement, common preflight contracts and isolation passed. Native Xray now reports the generated provider upstream as `direct`, confirming the explicit fixture-route correction was present. The owned provider bridge still recorded zero requests. The provider job failed with `RUNTIME_MEMBER_UNAVAILABLE`; this does not establish a successful group-delay probe, and no exact group-delay result was recorded.

The native URLTest path presents a separate fixture compatibility issue. Its probe uses HTTP `HEAD`, while the local fixture handler implements `do_GET`; the default handler response for unsupported `HEAD` is 501, and the fixture request counter remained zero. That evidence is consistent with the handler not serving the probe method, but it does not by itself prove that this explains the member-unavailable result. Commit `88060bf` adds support for `HEAD` with the same 204/503 behavior and a pure contract check. Remote verification remains **PENDING**.

The unknown-source correction `7bde084` is included in the pushed head, but this run does not establish that its new unit regression executed; no unit-test PASS is claimed. The result still does not establish provider/native convergence or the underlying runtime cause.

### Provider diagnostic acceptance — run 37934437529

Run [37934437529](https://github.com/Coxf0rd/fwrouter/actions/runs/37934437529), head `88060bf`, completed the singleton provider diagnostic **1 PASS / 0 FAIL / 0 SKIP**. Hosted runtime preflight, confinement and isolation passed; cleanup completed without errors. The real bridge returned HTTP 204, and the actual enable operation completed through exclusive Core selection and the native Mihomo path with the existing selection fence preserved.

The prior fixture findings are now confirmed and corrected for this diagnostic: generation had no explicit `DIRECT` rule for the test upstream, and the pinned Mihomo URLTest used `HEAD` while the local fixture only implemented `do_GET`. The `HEAD` request therefore received the unsupported-method response before the GET counter could increment. Commit `88060bf` adds the matching `HEAD` response behavior; this run passes the real provider scenario with that correction. These findings establish test-fixture defects for this path, not a historical WAN or general host-routing root cause. Unsupported TPROXY and host-only handoff limitations remain separately gated; no full-host parity is claimed.

The full 37-case functional cohort remains **PENDING** and has now been triggered by the documentation commit using `ci:validate-functional`. This singleton PASS does not promote the wider suite or Phase D.
