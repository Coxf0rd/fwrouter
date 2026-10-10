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

## Full affected and functional checkpoint — run 37934872853

Run [37934872853](https://github.com/Coxf0rd/fwrouter/actions/runs/37934872853), source `b580b2e`, completed the mandatory hosted attempt with **Phase D BLOCKED**. Static/bootstrap/focused infrastructure preflight passed. The qualified Mihomo child cohort passed **26/26**, isolated Docker Xray passed **1/1**, and the minimal application Xray create/delete readback diagnostic passed **1/1**. These scoped passes do not override the affected or functional failures.

The selected affected unit gate completed L0–L3 only: **1,112 passed / 129 failed** across 1,241 node statuses. The gate report is ineligible, execution is incomplete, and mandatory L4/L5 evidence is absent/incomplete; no deploy eligibility is granted. Suite-level evidence shows a shared isolation/profile blocker: 17 suite diagnostics contain `PermissionError: FWRouter tests deny process execution: subprocess.Popen`, while the plan has no available execution profiles and requires `hosted-isolated-compose` and `qualified-child-process`. Four provider-related suites abort during collection at `gate_plugin.py:104` with `KeyError: 'domain'` and collect zero tests. These are evidenced harness/profile and metadata collection failures; they do not establish that all 129 nodes are product regressions. Other unit failures contain varied assertion/fixture outcomes without one proven common source cause.

The full hosted application/browser cohort collected 37 cases and finished **23 PASS / 14 FAIL / 0 SKIP** in 237.775 s; the first run exited 1 and the repeat was correctly not run. The evidence CSV classifies 11 failures as member-delay/selector prerequisites, one as a browser assertion contract, one as a recovery-fence outcome, and one as a CAS-reconcile outcome ([failure analysis](FUNCTIONAL_FAILURES_37934872853.csv)). The primary provider case reached the real bridge (2 requests / 2 HTTP 204 responses) but the job ended `PROVIDER_LOCAL_VERIFICATION_FAILED`; the worker recorded member-delay HTTP 503 / `PROVIDER_CONNECTIVITY_UNCONFIRMED`. The underlying probe cause and any numeric delay result remain **UNKNOWN**; no numeric `0 ms` result is retained. The exclusive browser flow likewise stopped before its intended UI persistence assertions after a delay prerequisite failed. The browser Xray editor case submitted an accepted create request and received terminal success where the test expected failure; the source test contains no failure injection for that create. The recovery-fence case returned `provider_recovery_fence_unavailable`, and the CAS case did not expose the expected reconcile result. These are observed scenario outcomes, not evidence that all 14 failures are application regressions.

The end-to-end `test_provider_disabled_and_unknown_source_do_not_call_provider_or_patch` acceptance case **passed**, so the public unknown-source behavior is verified in this run. The new unit regression `test_unknown_source_discovery_returns_not_found_without_provider_io` has no execution status: `test_provider_configuration.py` aborted at collection with the `KeyError: 'domain'`, so neither it nor the legacy tests in that unit file are claimed as executed or passed.

Of the seven exact historical IDs that passed together in run [37904598829](https://github.com/Coxf0rd/fwrouter/actions/runs/37904598829), this run reports five current PASS: `test_runtime_topology_batch_matches_single_and_uses_one_observation`, `test_reconcile_endpoint_returns_contract_and_is_read_only`, both `test_state_routes` read-only checks, and `test_vpn_auto_reconcile_removes_stale_identity_and_recreates_one_stable_identity`. `test_legacy_subscription_get_is_read_only` and `test_vpn_auto_invalid_stage_keeps_database_and_active_xray_unchanged` were skipped/uncollected in this run. The earlier **7/7** remains valid historical evidence for that earlier run, not a current 7/7 claim or closure of the full baseline.

Receipt resources: functional job wall **237.775 s**, CPU user **76.002 s** / system **13.487 s**, maximum single-process RSS **192,968 KiB** (not aggregate container memory), and **22,632,974 bytes** owned temporary data before cleanup. Cleanup errors: **none**. The full cohort did not establish host-networking parity: TPROXY and nonlocal handoff remain NOT RUN; the previously logged errors (`operation not permitted` for both TPROXY listeners and `cannot assign requested address` for the nonlocal `172.18.0.1` bind) remain separate host capability limits. A functional PASS would not close those gates.

Phase E remains OPEN. No production deployment/restart, CD, merge, or Phase E advancement is claimed. See [LAST_FULL_CHECKPOINT.json](LAST_FULL_CHECKPOINT.json) for structured counts, exact historical-ID statuses and the per-gate boundary.

## Metadata gate checkpoint — 2026-10-10

Run [37963072861](https://github.com/Coxf0rd/fwrouter/actions/runs/37963072861), source `587e597`, completed the hosted metadata stage successfully: **9/9 exact selected nodes passed**, with zero failures/skips, exact node coverage, JUnit present, and owned temporary cleanup confirmed. The selected cases cover provider policy defaults/migrations, local no-I/O gating, recovery gating, the unknown-source no-I/O contract, and inventory behavior. Focused gate and acceptance contracts also passed **38/38** and **41/41**, respectively. Exact IDs and receipt references are in [METADATA_CHECKPOINT_37963072861.json](METADATA_CHECKPOINT_37963072861.json).

This is a scoped metadata-stage PASS only. It does not rerun or resolve the full affected/unit gate or full functional cohort; Phase D and the full functional gate remain **BLOCKED**, with the last full functional result still **23 PASS / 14 FAIL** (run 37934872853). Application, native, qualified-child, deployment, and Phase E acceptance are not claimed by this checkpoint.

## Numeric provider diagnostic — 2026-10-10

Run [37963610399](https://github.com/Coxf0rd/fwrouter/actions/runs/37963610399) passed the exact singleton application/native case: **1 PASS / 0 FAIL / 0 SKIP**. Both group and member native delay were **1 ms**; the publication callback verified the provider target. This does not prove the cause of the earlier full-cohort failures: a zero-delay result was not observed. Resources and scope are retained in [the structured checkpoint](PROVIDER_NUMERIC_CHECKPOINT_37963610399.json).

Next diagnostics are deliberately bounded: six explicitly isolated unit nodes and eleven prior provider-prerequisite cases, not a repeat of all 37 functional cases. Host observations in selected unit tests are explicit non-autouse fixtures; missing live-mode evidence remains unknown, never healthy. Their execution is pending the next hosted run. Phase D remains BLOCKED; no production changes, Phase E, CD, or L7 execution.

## Explicit host-observation fixture checkpoint — 2026-10-10

Run [37965069661](https://github.com/Coxf0rd/fwrouter/actions/runs/37965069661) passed all **6/6 exact selected unit nodes**, with zero failures/skips, JUnit, exact coverage, and owned cleanup. The non-autouse fixtures isolate DNS/protected-network discovery and leave unobserved nft mode explicitly unknown; dedicated native/discovery tests are untouched. See [receipt](HOST_FIXTURE_CHECKPOINT_37965069661.json). This is representative evidence only, not resolution of every subprocess-denial or the last full affected gate. The next hosted provider cohort contains precisely the eleven previous member-delay prerequisite failures.

## Browser failure-contract correction — source checkpoint

The valid create request previously expected failure without a failure trigger. The test now arms the existing one-shot candidate corruption hook, asserts a real pinned Xray native validation rejection and unchanged active/loaded state, then retries the same form and requires actual native proof. This is test-only; hosted execution is pending. A dedicated browser diagnostic stage rejects skipped receipts. Recovery-fence and CAS outcomes still lack branch-level evidence and remain open, without speculative product changes.

## Stopped checkpoint — 2026-10-10

The user requested a full current-state handoff after the active check. Run [37965465706](https://github.com/Coxf0rd/fwrouter/actions/runs/37965465706), source `cf6d2d0`, finished **2 PASS / 9 FAIL / 0 SKIP** over the fixed eleven-case provider cohort. Both passed cases reached real provider/Core/Mihomo paths; all selected cases completed setup and teardown. Cleanup errors: none. Detailed safe receipt is [PROVIDER_COHORT_CHECKPOINT_37965465706.json](PROVIDER_COHORT_CHECKPOINT_37965465706.json).

Numeric evidence now includes group delay **0 ms** in the confirmed-failure case. Three member HTTP 503 responses have immediately subsequent native proxy snapshots with **alive=true**, latest history delay **0 ms**, and history age **16 ms**. This establishes zero-valued successful native observations for part of the fixture/probe path; it does not prove every failure has that cause or justify treating arbitrary 503 as UP. No adapter normalization or runtime fix was attempted. Other cases reached recovery-fence-unavailable, a negative selector result, or browser response timeout; their exact root causes remain open.

Prepared source, NOT RUN: seventeen further explicit read-model fixture adoptions; bounded worker-only recovery/CAS branch observer and its pure contracts; exact two-node fence diagnostic. Browser failure/retry source correction was pushed in `cf6d2d0`, but its dedicated browser case was not selected in this cohort and remains NOT RUN. No new hosted run will start at this stopped checkpoint. Last full functional baseline remains **23 PASS / 14 FAIL**; last full selected affected gate remains **1112 PASS / 129 FAIL**, not superseded by narrow PASS. Phase D remains **BLOCKED**. Phase E/CD/PR/merge/production deployment were not started.

## Strict zero-delay regression checkpoint — 2026-10-10

Run [37969825404](https://github.com/Coxf0rd/fwrouter/actions/runs/37969825404), source `b1ebe8c`, passed **5/5 exact isolated L1 nodes**, zero failures/skips, exact JUnit coverage and owned cleanup. Infrastructure contracts passed **38/38 + 51/51**, isolation smoke **13/13**. See [safe receipt](ZERO_CHECKPOINT_37969825404.json). These are fake-only adapter regressions, not native/application acceptance.

Pinned Mihomo truncates successful latency to integer milliseconds; zero is legitimate. Its member delay route returns HTTP 503 for zero as well as errors. The adapter formerly discarded zero. The correction preserves valid zero, and resolves only the exact ambiguous member response through one bounded read-only same-member observation: exact URL, alive=true, valid zero history and timestamp within the probe/readback window. Arbitrary 503, missing/stale/global-only evidence and API errors remain unsuccessful/unconfirmed. No retry, mutation or fabricated latency is introduced.

Two isolated fixture corrections are included: generated background fallback health URLs use the owned bridge before generation/validation; negative connectivity closes the owned connection without an HTTP response, since the native default accepts HTTP statuses without an expected-status constraint. Provider API 503/429 fixtures remain separate. Neither correction is yet application-validated.

Next gate selects three exact recovery/CAS/browser scenarios plus seventeen explicit host-observation fixture nodes. The broad affected gate and functional 37 are not repeated yet. Recovery controller interface mismatch is a source finding awaiting the bounded native trace. Phase D remains **BLOCKED**.

Debian 12 userland/isolated Compose remains the existing one-service harness. Dependency-download/build/image-size instrumentation is implemented; measurements are pending. GHCR dependency-image publication/consumption remains **NOT IMPLEMENTED**, not an accepted performance improvement. Ubuntu host kernel parity, privileged host dataplane and L7 remain unverified. No production tests/deploy, Phase E, CD, PR or merge.

## Targets checkpoint — 2026-10-10

Run [37970549010](https://github.com/Coxf0rd/fwrouter/actions/runs/37970549010), source `2dc656b`, completed the target stage with **0/3 native diagnostic cases passing** and **12/17 read-model fixture nodes passing**. All selected native cases completed setup and teardown; the diagnostic reported cleanup success. The exact safe receipt is [TARGET_CHECKPOINT_37970549010.json](TARGET_CHECKPOINT_37970549010.json).

The five read-model failures share a test-isolation gap: they reached `external_source_observations.DEFAULT_SCRIPT_RUNNER.run`, then the isolation audit correctly denied `subprocess.Popen`. The existing explicit `isolated_host_observations` fixture isolated DNS and dataplane discovery but omitted the external-source script boundary. The source-only fixture correction now returns the service’s `EXTERNAL_SOURCE_PROBE_UNAVAILABLE` error payload with empty observation collections. It does not invent healthy or missing peers. An exact hosted rerun of the 18-node cohort, including a fixture contract node, is pending.

The native cohort still has three distinct failures: a browser/native-readback assertion, a provider-unavailable case returning `provider_recovery_fence_unavailable`, and a CAS-reconcile case that did not enter the expected reconcile branch. Nested `worker-service-logs.json` records two recovery captures with `exception_type=AttributeError`, `context_present=false`, and return line 117. The worker route passes a `MihomoHttpAdapter` instance as Core’s recovery controller; source confirms that adapter has no `get_state()` method, while `_capture_recovery_context` calls it before checking `active_target_id` and returns `None` from its exception handler. The recovery fixture misbinding is therefore confirmed by both source and the retained runtime trace.

The CAS case recorded `cas_committed=false` with `active_matches_selected=null`; its generation callback reported `applied=false` and `VPN_AUTO_SELECTION_PERSISTENCE_CAS_FAILED`, and no reconcile event followed. This is a failed/no-op callback path, not evidence that the intended CAS-miss reconciliation was reached. The browser failure remains unattributed. The safe event subset, indexed by testcase, is in [TARGET_CHECKPOINT_37970549010.json](TARGET_CHECKPOINT_37970549010.json); raw worker logs remain in the run evidence directory. No application behavior is marked corrected by this evidence alone.

The run measured a **1,961,517,169-byte** built image and **45.328-second** compose build; the base image was not present before build. Downloaded asset archives were 182,166,967 bytes for Chromium, 22,821,828 for Mihomo, and 20,744,877 for Xray. The raw evidence remains under `evidence/run_37970549010/`; the checkpoint contains counts and resource measurements without provider identifiers or credentials. Phase D remains **BLOCKED**; this narrow run does not replace the broader full-gate or functional baselines.

### Target fixture corrections prepared after the trace

Recovery and re-entry now construct a fresh production Core controller using the same factory, routing input and selection-fence capture as watchdog. The test-only raw HTTP adapter is no longer passed as a controller. Browser selectors now use the actual unique API inventory `subject_id`; client API operations and native identities still use `client_id`. The previous locator mismatch was a test defect, not evidence that the client disappeared.

The CAS scenario now establishes a real owned native `vpn-auto=DIRECT` precondition, verifies exact readback and unchanged persistent selection fence, then invokes the public Core selector switch in a bounded request thread. Only this isolated test endpoint can inject the fixed DIRECT target; it cannot choose arbitrary URLs/targets or write selection/provenance. The existing post-apply commit barrier advances only the monotonic revision and always releases/joins in finally. Assertions require actual apply, matched native readback, fresh reconciliation, final revision/provenance and no extra provider request. This tests the intended post-PUT branch rather than the no-op repair path.

These changes and the eighteen-node fixture cohort are **SOURCE ONLY / execution pending**. Fencing, native proof, rollback and product Core ownership are unchanged.

### Read-model fixture checkpoint — 2026-10-10

Run [37971905644](https://github.com/Coxf0rd/fwrouter/actions/runs/37971905644) passed the exact read-model cohort **18/18**, with no failures or skips. The native diagnostic stage was **NOT RUN** because its prerequisite acceptance-contract step had eight errors: 53 contract tests ran, and eight launcher registry assertions rejected the stale scenario inventory after the CAS test contract docstring changed. These were registry errors, not eight test assertion failures. The independent gate-contract step passed **38/38**. No native acceptance result is claimed by this run.

The static catalog was regenerated from source AST only. It contains the same **48 node IDs** and unchanged suite/level assignments as before: **37 functional L3** and **11 recovery L7**. The only catalog diff is the CAS scenario's updated contract text. This corrects the stale-registry cause for a future run; it does not retroactively turn the native stage into a run or acceptance PASS.

## Fixture acceptance and fail-closed catalog checkpoint

Run [37971905644](https://github.com/Coxf0rd/fwrouter/actions/runs/37971905644), source `9b3e305`, passed **18/18 exact read-model/isolation nodes**, zero fail/skip, JUnit and cleanup confirmed ([receipt](HOST_FIXTURE_CHECKPOINT_37971905644.json)). The external-source unavailable fixture correction is now executed, not merely prepared.

Native/application/browser cases were **NOT RUN**: the infrastructure suite completed 53 cases with 8 catalog errors (45 passed), because the changed CAS contract description had not been regenerated in `scenarios.json`. Preflight blocked before expensive provisioning/build/native launch. Static registry regeneration changes only that description, preserving the exact 37 functional/11 separately gated L7 IDs and levels. No safety gate was bypassed. A corrected target run is pending; the previous native 0/3 result is not superseded. Phase D remains BLOCKED.

## Target checkpoint — 2026-10-10

Run [37972236339](https://github.com/Coxf0rd/fwrouter/actions/runs/37972236339), source `b51fab9`, passed the exact read-model cohort **18/18**, zero failures or skips, exact-node coverage and owned cleanup. The bounded hosted native cohort completed **1 PASS / 2 FAIL / 0 SKIP** with no cleanup errors. See [safe receipt](TARGET_CHECKPOINT_37972236339.json).

The typed provider-unavailable case passed: its HTTP 503 remained an unknown provider outcome and was not treated as a failed member. The browser case failed earlier at the client inventory lookup: the test expected one item matching its test identity but observed none, before the aggregate subscription-group alias edit. The aggregate alias behavior is therefore not established by this run.

The Core CAS scenario reached and completed the intended reconciliation path. Its worker events show a successful initial commit, `cas_committed=false` with `active_matches_selected=true`, a subsequent successful commit, and a reconcile event with no error code. The test then failed because the selector response omitted `selected_runtime_target` while its assertion compared that missing value with the observed active runtime target. This is a result-contract/assertion mismatch, not evidence that reconciliation was skipped. Retained evidence records only event names, commit flags and safe status fields.

The hosted build measured a 1,961,524,491-byte image and 41.477-second Compose build; the base image was absent before build. The application process receipt reports 24.93 seconds wall time, 110,892 KiB maximum process RSS and 1,910,478 owned temporary bytes before cleanup. These are bounded process/job measurements, not aggregate container or host-resource claims. The wider functional and affected-unit gates remain unresolved; Phase D remains BLOCKED.

### Browser contract attribution and next bounded check

Xray create with an email creates a persisted subscription account/profile. Settings intentionally hides the compatibility subject and renders a synthetic aggregate row; the prior test incorrectly sought that compatibility UUID. The browser fixture now first enables the real owned provider target, maps the rendered group by exact persisted subscription token/account and requires every enabled group member in native loaded identities. It retains true validation failure/retry, edit and deletion assertions through actual UI actions.

Source inspection exposes a separate potential product gap: group alias save uses the existing subject-alias endpoint, which only searches ordinary persisted `subjects`. Synthetic group IDs have no row there. The next hosted run retains the success assertion to reproduce this behavior; no assertion is removed or changed to bless the error. No product group-alias change is made without that evidence. CAS now compares exact readback with the required nonempty canonical `auto_transition.selected_runtime_target` field. Execution remains pending.

## Target checkpoint — 2026-10-10, alias behavior reproduced

Run [37996472774](https://github.com/Coxf0rd/fwrouter/actions/runs/37996472774), source `e64076b`, passed the exact read-model cohort **18/18** and completed the native cohort **2 PASS / 1 FAIL / 0 SKIP**, with exact coverage and clean owned-resource teardown. See [safe receipt](TARGET_CHECKPOINT_37996472774.json).

The provider-unavailable/503 case passed with the provider outcome classified as unknown. The CAS-miss test also passed, including its runtime readback and reconciliation assertions. The browser test reached aggregate subscription-group alias save; the existing alias API returned `SUBJECT_NOT_FOUND` for the synthetic group subject. This confirms the source-level mismatch: the endpoint delegates to `update_subject_alias`, which only looks up persisted `subjects`, while the Settings group is synthesized from subscription-backed Xray rows. No product alias fix is included in this checkpoint.

The hosted build measured a 1,961,529,415-byte application image and 38.472-second Compose build; the pinned base image was absent before build. The application process receipt reports 33.859 seconds wall time, 192,032 KiB maximum process RSS and 2,093,016 owned temporary bytes before cleanup. These are bounded process/job measurements, not aggregate container or host-resource claims. The wider functional and affected-unit gates remain unresolved; Phase D remains BLOCKED.

### GHCR dependency-image proposal review

The current proposal is structurally compatible with the one-service Compose harness: keep Compose isolation and the per-run source build, and make a separate source-free image from pinned runtime dependencies. The current acceptance Dockerfile combines apt packages, hash-locked Python dependencies, and provisioned Xray/Mihomo/Chromium with copies of FWRouter backend/UI/tests/source. A split therefore needs an explicit dependency-only build context/Dockerfile containing no application tree, followed by the existing fresh source-context build consuming only a reviewed immutable dependency-image digest.

Before treating that split as repeatable or faster, pin the Debian package set (the current Dockerfile uses floating `apt-get update` package indexes), keep Python requirements hash-locked, and verify asset digests/versions when assembling and consuming the dependency image. A trusted publisher workflow must be distinct from PR/fork consumers; only that publisher receives package-write access. Consumer jobs should remain read-only, verify the exact digest and expected dependency labels before startup, and retain the current source-revision/runtime-input checks. Compare cold and warm end-to-end provisioning/build time, image transfer bytes, cache-hit evidence and resource use against the existing measured baseline; the current 38.472-second build and 1.96 GB image are not evidence of a GHCR benefit. GHCR publication/consumption remains NOT IMPLEMENTED, and there is no workflow or Dockerfile change in this review.

### Exact subscription-group alias correction — source checkpoint

Hosted run 37996472774 proves that Settings sends the aggregate group ID to the
existing alias endpoint, which previously searched only persisted subject rows.
The source correction delegates exact token-hash groups to the subscription-profile
metadata owner. Resolution, single-account/client cardinality, name persistence
and sanitized audit share a short SQLite writer transaction; audit failure rolls
back both labels. Clearing restores the existing slug-derived default. Identical
labels do not update or audit. The existing projection cache is invalidated only
after commit. Settings/client/presence projections prefer the canonical per-client
name, retaining group IDs, native identities and member intent unchanged.

Four focused isolated L2 regressions cover persistence/projection, no-op/clear,
unknown/ambiguous/multi-client rejection, audit rollback/cache preservation and
ordinary subject alias behavior. Native browser acceptance retains the real edit
and delete assertions. Local verification is AST/JSON, source catalog (48 unchanged
acceptance IDs), whitespace and clean-surface only. Actual tests are pending the
next hosted target run; no production application/runtime deployment occurred.

### Fail-closed preflight and label regression — run 37997677290

Source 72ce784: the existing 18-node fixture gate returned 17 PASS / 1 FAIL,
zero skips, exact coverage and clean owned teardown. The distinct account/client
label regression was detected: account name incorrectly replaced a valid
per-token client name. The existing assertion is retained. Group projection
must prefer subscription_clients.display_name, with the existing derived label
as fallback; the single-client alias operation updates the account/client pair.
The four new alias cases were NOT RUN because their prior step failed.
Preflight independently rejected a new blank line at EOF. Native/application/
browser diagnostics were NOT RUN and no image was built. No skipped gate is
reported as PASS. Raw artifacts are preserved in evidence/run_37997677290/.

### Hosted alias correction and target checkpoint — run 37997903110

Run [37997903110](https://github.com/Coxf0rd/fwrouter/actions/runs/37997903110), source `6fcfaee`, completed successfully. The exact host-independent read-model cohort passed **18/18**; the four subscription-group alias regressions passed **4/4**; and the three hosted process-backed native/browser scenarios passed **3/3**. All cohorts had exact node coverage, zero skips and successful owned cleanup. Infrastructure preflight also passed: gate contracts **38/38**, acceptance contracts **53/53**, isolation smoke **18/18**, workflow lint and source/diff checks. See [safe checkpoint](TARGET_CHECKPOINT_37997903110.json); raw artifacts remain under `evidence/run_37997903110/`.

The previous account/client label regression is corrected: the aggregate group projection now uses the per-client display label, preserving its distinction from the account label and token. Hosted alias tests cover canonical account/client update and Settings projection, unknown/colliding/multiclient rejection, audit failure rollback/cache preservation and unchanged ordinary-subject alias behavior. The native browser editor scenario now passes alongside provider-503-as-unknown and the actual Core CAS-miss reconciliation case. This is source-level hosted proof only. It does not claim stock-Docker runtime parity, host dataplane/provider traffic, deployment or live-state acceptance; full affected and functional gates remain separate.

The hosted application image was **1,961,545,265 bytes** and Compose build took **49.231 s**, with the pinned base image absent before build. The application process receipt reports **33.81 s** wall time, **192,348 KiB** maximum process RSS (not aggregate memory), and **2,136,206 bytes** owned temporary data before cleanup. Runner snapshot was 4 CPUs, 15 GiB memory, 145 GiB root disk with 85 GiB free, Docker 28.0.4 and Compose 2.38.2. Phase D remains BLOCKED on the broader mandatory gates.

### Provider/Core diagnostic cohort — run 37998250152

Run [37998250152](https://github.com/Coxf0rd/fwrouter/actions/runs/37998250152), source `dcf86d0`, executed the fixed 11-node provider cohort and completed **5 PASS / 6 FAIL / 0 SKIP**. Every selected node completed setup and teardown. Static/workflow/preflight checks passed, including gate contracts **38/38**, acceptance contracts **53/53**, and isolation smoke **18/18**. The native process profile was confined and pinned; cleanup reported no errors, owned resources removed, and temporary artifacts removed. See [safe checkpoint](TARGET_CHECKPOINT_37998250152.json); downloaded artifacts are preserved under `evidence/run_37998250152/`.

The hosted native observations confirm the strict zero-delay path on successful target verifications: group-delay returned HTTP 200 with `delays_ms=[0]`; a subsequent proxy-state read returned `alive=true` with latest history delay 0 only 18–19 ms old; the member-delay endpoint returned its pinned generic HTTP 503; and the callback still reported `provider_target_verified`. This does not turn the separate injected 504 no-response into a remote member-down observation. Three emergency-Direct cases returned `action=none`, `outcome=failed`, and no effective override before their reentry/race assertions. One refresh summary in each worker also reports `stage=preflight`, `VPN_AUTO_NO_ELIGIBLE_ALTERNATIVE`, `applied=false`, and `runtime_verified=false`, but current diagnostics do not correlate that refresh record to the later recovery confirmation or emergency apply. The cause and exact phase of the failed emergency apply are therefore **UNRESOLVED**; no systemd or nft failure evidence was captured. Do not attribute the returned failure to Core preflight until an operation-correlated trace proves the phase.

The stale-selector race node failed earlier than its intended fence assertion: the public selector endpoint returned HTTP 200 with top-level `ok=false`, `applied=false`, and the same active provider target. No selection-fence commit was recorded for that node, so the competing selector did not win in the evidence; pytest retained only a truncated response, leaving the exact selector error and whether the seeded candidate pool is sufficient unresolved. The concurrent-exclusive and incarnation reentry tests also stopped at the same emergency-Direct preflight failure, so neither fence outcome was exercised.

The Xray generation test did replace the native child incarnation as intended, but its job stopped with `XRAY_GENERATION_RUNTIME_READBACK_FAILED`, `stage=xray_generation`, and `last_good_retained=true`. Source has an exact-readback guard that rejects mismatch before the later stale-before-publication branch. The runtime was therefore rejected, but the observed error taxonomy differs from the test's expected `XRAY_GENERATION_STALE_BEFORE_PUBLICATION`; the exact field causing strict readback mismatch remains unknown. The browser exclusive test timed out waiting for its response predicate. UI source URL-encodes `source_ref`, while the test predicate compares the unescaped `src:` value; this is a likely test matcher encoding defect, but the endpoint request/result was not independently captured.

Three typed provider API error cases passed while preserving unknown outcomes, and the main provider discovery/exclusive-intent/native-Mihomo path passed. The unknown/recovery and fence failures remain distinct from these successful paths. The 37-case functional suite, affected gate, qualified child suites and L7 were **NOT RUN** by this stage. Phase D remains **BLOCKED**; no production changes or deployment are claimed.


### Recovery failure phase correction and bounded diagnostic — 2026-10-10

Review of run 37998250152 found that the provider verification observer records refreshes without linking them to an individual recovery decision. Therefore its `VPN_AUTO_NO_ELIGIBLE_ALTERNATIVE` `stage=preflight` event cannot be assigned to the third failure confirmation or the subsequent emergency apply. The three failed public responses prove only `outcome=failed`, `action=none`, and no effective override; systemd/nft failure and the exact recovery/apply branch are **NOT ESTABLISHED**. This corrects the earlier attribution above.

A fixed two-node hosted recovery diagnostic is prepared under marker `ci:validate-recovery`: the emergency-Direct failure scenario and the stale-selector race scenario. Its qualified-worker observer tags phases and a bounded numeric operation sequence, records only whitelisted result codes/statuses/booleans/counts and caught exception type/source line, and reraises unchanged. It does not log exception text, request identifiers, configuration or secret values. The browser response predicate now applies `urllib.parse.quote(source_ref, safe="")`, matching the UI's existing `encodeURIComponent`; response status and persistence assertions remain intact. These source changes have not been committed or run remotely. Exact row attribution, phase result, and browser correction remain unverified until the fixed hosted diagnostic executes.

Run 37999499394 (source `7a14286`) did **not** execute the recovery diagnostic: preflight's contract suite had 52 PASS / 1 FAIL because its fixed CLI-choice assertion omitted the newly registered `recovery-diagnostic` suite. The native diagnostic and later jobs were skipped; this is a contract-registration failure, not a recovery scenario result. The Xray incarnation test's prior expectation is also corrected in source: after the test proves a new Xray PID and a different runtime incarnation at the `xray_applied` barrier, the exact worker refresh code is `XRAY_GENERATION_RUNTIME_READBACK_FAILED` (with `last_good_retained=true`), before the later stale-publication branch. The revised test keeps public failure, prior snapshot, loaded identity and active-runtime checks. These contract and assertion corrections are uncommitted and have not run; the two recovery nodes remain **NOT RUN**.

### Recovery diagnostic checkpoint — run 37999901823

Run [37999901823](https://github.com/Coxf0rd/fwrouter/actions/runs/37999901823), source `999c898`, passed preflight (gate contracts **38/38**, acceptance contracts **54/54**, isolation smoke **18/18**) and executed the exact two recovery nodes. Both failed with setup/teardown passing and exact node coverage. See [safe checkpoint](RECOVERY_CHECKPOINT_37999901823.json); retained hosted artifacts are under `evidence/run_37999901823/`.

The emergency-Direct trace now proves a real `_run_pipeline_for_state` attempt returned `ok=false`; the override and emergency recovery consequently returned failed/action none, and the API remained `provider_recovery_pending`. A separate refresh `stage=preflight` record is not correlated to this recovery operation and is not its proven cause. The observer on this run captured only the outer Core `ok` flag, so the dataplane stage and error code remain unknown. The observer now has a narrowly allowlisted capture for the existing Core result's top-level stage/capability/enforcement and nested dataplane code/stage fields; messages and details remain excluded. It has not run remotely.

In the selector-race node, the actual public competing selector returned `ok=false`, with operation-correlated `provider_operation_busy` and a selected member present. No selection commit occurred. The test held the provider HTTP probe while the provider handoff's operation reservation remained active, so this run did not create a selector winner or exercise the stale-handoff fence. This is not evidence to bypass the reservation; a true race needs an eligible non-provider alternative or a different controlled interleaving that leaves the reservation contract intact. Provider failure recovery and stale-fence success remain **UNVERIFIED**. The application process used 10.954 seconds wall time, 111,728 KiB process high-water RSS and 1,366,226 owned temporary bytes; cleanup reported no errors and removed owned resources. Broader provider/browser/Xray cohorts, qualified child suites, full functional and affected L0-L5 gates were **NOT RUN**. No production source change or deployment is claimed.

The follow-up worker trace in run 38000419404 identifies the emergency-Direct Core failure as `operation=check`, `stage=check`, `error_stage=check`, and `error_code=NFT_NOT_AVAILABLE`, with `dataplane_capability=nft_owned_table` and `traffic_enforcement_guaranteed=false`. This proves failure in the dataplane check phase; it does not reveal the underlying `ScriptRunnerError` or prove nftables itself was unavailable. The selector case again returned `provider_operation_busy` without a selector commit. The kernel preflight now being prepared is a separate hosted check of actual namespace capabilities, nft syntax/apply/readback/cleanup, policy-route primitives, and exact copied script hashes. Its result is **NOT RUN**, and it does not claim Core dataplane or packet-path acceptance.

### Kernel preflight contract correction — run 38034478114

Run 38034478114 used source `49770beeecbb99503bc2dca47b050b117ea329a9`. Manifest validation passed, gate contracts passed **38/38**, isolation smoke passed **18/18**, and acceptance contracts returned **54 PASS / 1 FAIL**. The sole failure was the fixed CLI `--suite` choice tuple, which did not yet include `kernel-preflight`; the exact choice was added while retaining the strict tuple assertion. The native kernel preflight and all later workflow steps were **NOT RUN**. See [safe checkpoint](KERNEL_PREFLIGHT_CONTRACT_CHECKPOINT_38034478114.json); raw hosted artifacts remain under `evidence/run_38034478114/`.

### Kernel preflight stopped-container inspection — run 38034649314

Run 38034649314 built the kernel image, then stopped before starting its container. The stopped-container validator reported only the grouped reason `container has forbidden capabilities, devices, or host namespaces`; its prior receipt did not retain the exact values, so this run does not establish whether Docker used `CAP_NET_ADMIN`, an extra capability, or another predicate. Kernel commands and native/application tests were **NOT RUN**. Owned resources and temporary artifacts were removed. Compose build measured **47.641 s** and **1,970,127,968 bytes**; the diagnostic process took **53.21 s** with **74,648 KiB** maximum process RSS (process high-water mark, not aggregate memory). The next source change adds a bounded allowlisted confinement receipt and per-predicate reasons, accepting only the exact singleton `NET_ADMIN` or `CAP_NET_ADMIN` spelling for this profile; normal acceptance remains capability-free. See [safe checkpoint](KERNEL_PREFLIGHT_INSPECT_CHECKPOINT_38034649314.json); raw evidence remains under `evidence/run_38034649314/`.

### Hosted kernel primitive preflight — run 38035140899

Run [38035140899](https://github.com/Coxf0rd/fwrouter/actions/runs/38035140899), source `045eac05e4ebd7693d6e6e7fe3b1baf00fb908d7`, passed the isolated kernel primitive preflight. The stopped-container receipt confirms exactly `CAP_NET_ADMIN` with `CapDrop=ALL`, UID/GID `0:0`, a read-only root, and no privileged mode; runtime checks confirmed the effective capability and a network namespace distinct from the host. Debian packages were nftables `1.0.6-2+deb12u2` and iproute2 `6.1.0-3`. A real nft syntax check, scratch TPROXY rule apply/readback, policy-route readback, baseline nft object preservation, and cleanup all passed. The four copied dataplane scripts matched source hashes and executable mode. No application tests ran in this kernel-preflight stage, so this establishes kernel primitives only, not successful Core apply/recovery or packet forwarding. The build measured **46.531 s** and **1,970,138,708 bytes**; the base image was absent before build. See [safe checkpoint](KERNEL_PREFLIGHT_CHECKPOINT_38035140899.json); raw hosted artifacts remain under `evidence/run_38035140899/`.

### Kernel-dataplane preflight contract failure — run 38035841556

Run [38035841556](https://github.com/Coxf0rd/fwrouter/actions/runs/38035841556), source `55dbbce`, stopped in preflight: gate contracts passed **38/38**, isolation smoke passed **18/18**, and acceptance contracts reported **55 PASS / 1 ERROR**. The fixed CLI-choice contract parses `choices` with `ast.literal_eval`; its tuple contained the `_KERNEL_RECOVERY_SUITE` name instead of a literal string, so the test raised before the kernel/application stage. I changed that entry to the exact literal while preserving the fixed-choice assertion. Kernel and application tests were **NOT RUN** in this attempt. Raw evidence remains under `evidence/run_38035841556/`.

### Kernel-dataplane PATH failure — run 38035930948

Run [38035930948](https://github.com/Coxf0rd/fwrouter/actions/runs/38035930948), source `52a4b2f`, passed kernel preflight but the two-node recovery suite failed **2/2**. The emergency-Direct case reached the genuine Core dataplane check and returned `NFT_NOT_AVAILABLE`; the hosted kernel receipt proves nftables was installed at `/usr/sbin/nft`, while the acceptance worker environment used `PATH=/opt/fwrouter-test/bin:/usr/bin:/bin`. The check script therefore could not locate the installed tool. The fix adds `/usr/sbin:/sbin` only to workers whose validated profile is `hosted-kernel-dataplane` or `hosted-kernel-packet`; the normal worker PATH is unchanged. The selector interleaving produced a real competing selector success and revision advance, but its earlier provider job failed local verification before the stale handoff outcome; this run does not pass the race assertion. See raw JUnit and worker artifacts under `evidence/run_38035930948/`. No production runtime was changed.

### Real Core apply and exact selector competition — run 38036508542

Run [38036508542](https://github.com/Coxf0rd/fwrouter/actions/runs/38036508542), source `cfc5229`, passed hosted static/bootstrap contracts and kernel qualification, then returned **0 PASS / 2 FAIL / 0 SKIP**. The PATH correction allowed real Core global VPN apply and native enforcement/readback. The Emergency Direct test stopped on a test-contract error: it expected derived `vpn_policy_required` in the replaced applied preflight object, whereas `apply.py` persists the durable requirement in `manifest.summary.requires_vpn_policy_routing`. Assertions now use that exact canonical field; runtime enforcement and contour proofs remain mandatory. Emergency Direct/re-entry were not reached in this run.

The competing ordinary Core selector really applied, advanced revision and retained active DB identity; the older provider generation hit exact `XRAY_GENERATION_STALE_BEFORE_PUBLICATION`, remained unverified/unapplied/unpromoted and retained last-good member/revision. The final assertion incorrectly compared a raw display name to native canonical logical runtime identity, which includes an ID suffix. It now requires exact `get_logical_runtime_name(server_id)` mapping, not a prefix match. End-to-end race acceptance is still pending the corrected run. Raw JUnit, native/process/worker logs and receipt are preserved under `evidence/run_38036508542/`; cleanup was confirmed. Neither failed test is counted as PASS, and broad gates were not repeated.

### Real Core Emergency Direct and independent selector fence — run 38036768080

Run [38036768080](https://github.com/Coxf0rd/fwrouter/actions/runs/38036768080), source `37373a6`, passed static/bootstrap/infra and actual kernel qualification, then **2 PASS / 0 FAIL / 0 SKIP**. The first node proves real Core global VPN check/apply/readback, policy-disabled confirmed recovery to effective Emergency Direct while retaining desired VPN, failed connectivity re-entry remaining Direct, and verified same-member VPN re-entry with native config/listener/enforcement parity and zero provider API calls in the asserted recovery window. The second node proves a permitted independent ordinary Core selector actually wins during the held provider probe, advances revision, and remains the DB/native target; the older provider generation is rejected by exact `XRAY_GENERATION_STALE_BEFORE_PUBLICATION`, with applied provider member/revision and last-good retained. Same-source provider reservation remains enforced. The tests use actual application/native paths; assertions require the internal typed fence rather than accepting any generic failed job.

This is **Kernel Core/Container Native acceptance**, not external-LAN packet forwarding acceptance or Debian Host/systemd/reboot parity. Qualified/full37/affected/historical/L7 jobs were skipped by the narrow stage and are not counted as PASS. Owned resources were removed. Image **1,970,152,617 bytes**, cold Compose build **62.333 s**, application wall **24.753 s**, CPU user/system **14.961/2.031 s**, max process RSS **113,276 KiB** (not aggregate), owned temporary bytes **2,034,933**. See [safe checkpoint](CORE_RECOVERY_CHECKPOINT_38036768080.json); raw receipts/JUnit/logs remain under `evidence/run_38036768080/`. Phase D remains **BLOCKED** on packet, broader and reproducibility gates.

### Minimal packet source checkpoint — hosted execution pending

The existing harness now registers one distinct L3 `packet-diagnostic` node, without expanding the original 37 functional or 11 L7 cases. Three Debian roles share reviewed dependency layers and fresh commit source; two internal networks connect LAN client, Core/router and native synthetic VLESS/local TCP/UDP/DNS endpoint. Only router adds NET_RAW for bounded Ethernet IPv4 header capture; all roles retain exact network/route/profile/capability/resource checks. Separate guards deny embedded Docker DNS and non-fixture egress; negative router output/forward probes require actual named drop-counter deltas. Positive phases require real Core `fwrouter_direct`/`fwrouter_vpn_full` counters, exact current endpoint observations and capture deltas. Global VPN uses full-VPN ports 5204/5205, not selective counters. The VIP service connection after VLESS is endpoint-local; its source is the VIP, while WAN capture proves the router-to-endpoint transport hop.

Review corrected profile ownership assumptions, nested generated nft set braces, capture privilege-drop configuration, event-driven readiness, and strict counter/packet attribution. Source-only AST/JSON/generated-text checks and clean-tree surface passed. No local test, Docker/native/application import, production mutation or deployment ran. Actual packet/network/cleanup acceptance remains NOT RUN until the controlled hosted stage returns JUnit/native evidence. DNS/5353 is a local fixture, not DNS/53 capture, DHCP or IPv6 acceptance. Broad gates, GHCR consumer/publication and Phase D acceptance remain open.

### First packet hosted attempt — run 38052547436

Run [38052547436](https://github.com/Coxf0rd/fwrouter/actions/runs/38052547436), source `f2ec610`, stopped at manifest validation: the packet file declared `execution_profile=hosted-kernel-packet` but the gate registry still accepted only the previous profiles. This is a confirmed test-infrastructure registration defect. Native application and packet jobs were **SKIPPED/NOT RUN**; no packet PASS is claimed. The retained manifest diagnostic is under `evidence/run_38052547436/`. Correction must register this profile narrowly, preserve fail-closed exact-path qualification and route execution/defer through the hosted Compose launcher rather than ordinary host pytest.

Source review additionally corrected the client negative FORWARD probe to the fixed internal endpoint TCP/22, whose existing Core management gate remains DIRECT even in global VPN; the former port 65000 could enter TPROXY and test a different path. Router-origin negative OUTPUT remains port 65000. No application rules or production listeners were changed.
