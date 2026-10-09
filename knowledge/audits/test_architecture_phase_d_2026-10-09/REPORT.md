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
