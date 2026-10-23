# L6 full regression workflow

## Scope and status

L6 is a scheduled or explicitly dispatched full safe-manifest qualification. It is separate from the stable push/PR required check and never deploys. L7 recovery/host acceptance is excluded. The parallel implementation and complete independently replayable bundle have passed the dated branch qualification below. Branch qualification remains distinct from fresh actual-main/release qualification; source inspection, local tests or a previous SHA cannot substitute for a required exact-head run.

## Trigger and trust boundary

`.github/workflows/test-full-suite.yml` runs nightly on `main` at 04:17 UTC. Manual dispatch requires the exact text `RUN L6`, an explicit `main` or `branch` mode, and a 40-character expected SHA matching the checked-out/event SHA. Scheduled runs are accepted only on `refs/heads/main`. Main-mode dispatch is accepted only on `main`; branch mode produces `branch-regression-non-promotional` evidence and cannot satisfy main/release qualification. CI has `contents: read`, uses GitHub-hosted `ubuntu-24.04` runners and pinned Actions, and receives no production credentials or deployment authority. Checkout credentials are not persisted.

The plan comes from the checked-out immutable source and complete safe manifest. L7 remains outside the selected plan. Strict consumers bind evidence to the repository, workflow, event/ref, source SHA, run ID, attempt, plan/manifest, required profiles and exact upstream job result set. Missing, skipped, cancelled, failed, stale, duplicated, incomplete or cross-attempt evidence blocks eligibility.

## Execution graph

The workflow has eight logical job definitions and 19 runner jobs after matrix expansion:

| Logical job | Runner jobs | Work and dependency |
| --- | ---: | --- |
| `plan` | 1 | Full safe-manifest plan, trigger/SHA checks, static and isolation preflight; publishes the immutable one-run plan. |
| `build` | 1 | Verifies the plan, builds the backend wheel and validates static UI source once. It runs in parallel with the routine shards after planning. |
| `unit` | 8 | Manifest-owned routine/contract shards; at most four run concurrently. Each shard reports exact suites and observed node IDs, results and resource/cleanup evidence. |
| `hosted` | 2 | Independent ordinary functional/browser runs with separate runner state and receipts. |
| `provider` | 2 | Independent provider/browser qualification runs. |
| `packet` | 2 | Independent kernel packet/fail-closed qualification runs. |
| `native` | 2 | Separate qualified-child (QCP) and Docker-Xray native profiles. |
| `l6` | 1 | Requires every producer result, reconstructs the routine report, runs the existing strict aggregate and safe-recovery qualification, prepares and independently verifies the final bundle, then publishes it. |

The count is `1 + 1 + 8 + 2 + 2 + 2 + 2 + 1 = 19` runner jobs. Hosted/provider/packet and native jobs depend on plan/build, not on routine completion. The final job is the sole aggregate/publisher and rejects a missing or unexpected child artifact, wrong identity, failed/skipped upstream job, incomplete routine union, fewer than two independent hosted-family receipts, missing native proof, or failed safe recovery. The two repeats must have independent run-owned state/nonces rather than duplicated copies of one receipt.

The full manifest and required L5 regression anchors are proven by the exhaustive, disjoint routine-shard union; anchors remain under their primary manifest owners and are not rerun merely for a second coverage receipt. L6 routine reporting records selected/collected/executed/unique IDs and outcome states, plus level/domain/group mapping. It distinguishes logical unique-case coverage from total executions across repeats and does not inflate test counts from shell commands or file markers. Job summaries provide visibility; only the strict sealed receipts and final replay establish eligibility.

## Evidence lifecycle

| Evidence class | Retention | Contents and limits |
| --- | ---: | --- |
| Cross-job plan/build/routine/profile handoffs | 1 day | Run/attempt-bound, allowlisted sealed inputs required by the final validator. Runner-only logs, raw profile payloads, packet captures, raw configs and disposable state are not uploaded as handoff evidence. |
| Failed/incomplete L6 diagnostics | 3 days | Bounded, redacted diagnostics and whitelisted receipts; explicitly not qualification evidence. Hard cancellation can prevent the final job from running, so diagnostics are best-effort in that case. |
| Complete L6 evidence bundle | 7 days | Final verified bundle with source/plan/build identity, original sealed producer evidence, routine shards, required native/runtime inputs, strict aggregate, safe-recovery receipts, visibility report and content hashes sufficient for offline replay. It is not only a short summary. |

The final artifact name is `fwrouter-l6-evidence-<SHA>-<run_id>-<attempt>`. It is deliberately distinct from the main-CI evidence artifact and carries `deploy_eligibility: false`. A branch-mode bundle is useful for non-promotional qualification only. The bundle is published only after all mandatory checks and safe recovery pass and its offline verifier accepts the exact run. Bundle build, verification or publication failure fails the final L6 job; it cannot convert failed qualification into PASS. No cleanup workflow, `actions: write`, privileged service or additional deployment component is introduced.

Offline verification must use a separately trusted, clean checkout at the expected exact SHA and independently supplied repository/workflow/event/ref/run/attempt expectations. Bundled verifier source is evidence to hash and inspect, not code to execute. An internally consistent content manifest proves file integrity, not GitHub origin or freshness. A retained bundle also does not establish that the corresponding GitHub artifact is still available; consumers that require an unexpired artifact must verify availability and identity from GitHub metadata. Missing or expired evidence is a hard failure, never a fallback to a green badge or stale copy.

## Promotion boundary and remaining qualification

Release qualification requires a fresh successful L6 run for the exact current `main` SHA **and** an independent successful actual-main CI run for that same SHA. L6 does not execute L7 or prove Debian host/systemd/reboot parity, production health, external client behavior, or deployment readiness. A branch-mode run cannot substitute for the main run. The branch evidence below does not close actual-main qualification. After an owner merge, verify a fresh full L6 run and independent mandatory main CI for the exact resulting main SHA. Every subsequent source or documentation change still requires qualification of its own final HEAD; the PR records that current result. No production action is authorized by this document.


## Dated branch qualification — 2026-10-23

Source `c2f9defbfee5fbb654646c50c1ac33d2a8cd3007`, attempt 1: [mandatory PR CI 39952092975](https://github.com/Coxf0rd/fwrouter/actions/runs/39952092975) passed all 16 runner jobs; [full L6 39952099257](https://github.com/Coxf0rd/fwrouter/actions/runs/39952099257) passed all 19 runner jobs. Both downloaded final bundles passed offline replay from a separately trusted clean exact-source checkout. Independent source/security review found no implementation blocker; eleven independently resealed adversarial L6 bundles were rejected, including missing/duplicated repeats, attempt/source/run mismatch, manifest/plan/receipt modification and repeat-count downgrade. The stale pending-run documentation was corrected in the follow-up; its final HEAD must also pass both workflows and review before readiness.

The manifest contains 187 files: 186 safe L1–L4 files and one excluded L7 file. All 186 safe files are observed in both qualifications. L6 reports 2,658 unique observed test IDs and 2,701 executions, all PASS, with zero FAIL/SKIP/BLOCKED/NOT RUN. The 43 additional executions are the second independent ordinary/provider/packet repeats; six distinct run-owned nonces and both native profiles were verified. Observed L1/L2/L3/L4 unique coverage is 1,518/359/701/80; L3 has 744 executions. L0 remains static checks, ten L5 anchors are coverage over primary cases, and L6 is the full regression policy. Existing Node scripts with assertions but no nested named tests contribute one observed TAP file case each; no assertion count is invented. Exact IDs and original receipts are in the complete bundle, not inferred from file totals.

| Observation | Mandatory PR CI, same source | Full branch L6, same source |
| --- | ---: | ---: |
| Runner jobs | 16 | 19 |
| Run wall seconds | 446 | 462 |
| Earliest-job to last-job span seconds | 443 | 458 |
| Sum runner-minutes | 31.0 | 46.07 |
| Artifacts / API-reported bytes | 16 / 4,842,579 | 19 / 4,641,542 |
| Unique IDs / total passing executions | 2,658 / 2,658 | 2,658 / 2,701 |

Both workflows ran concurrently on shared GitHub-hosted capacity, so setup/queue/cache/network variation is included; this is not a controlled speedup or billing experiment. Historical monolithic L6 [39755095242](https://github.com/Coxf0rd/fwrouter/actions/runs/39755095242), source `d313dcf`, took 2,105 runner seconds in one job (35.08 runner-minutes), but its source, coverage granularity and registered inventory differ. It is timing context, not a matched causal comparison or current release evidence. Parallelism purchases independent failure isolation, explicit test/profile visibility and concurrent hosted repeats at higher summed runner cost; no percentage improvement is claimed. The affected-CI V2/V3 comparison is recorded separately in [CI_V3.md](CI_V3.md).

Per-job GitHub step timings distinguish test execution from setup and artifact transfer; routine sealed receipts include bounded process high-water RSS, runner memory/disk snapshots and cleanup. These are not global cgroup/RAM/CPU peaks or measured network-transfer bytes. One complete successful L6 bundle survives seven days; the eighteen temporary producer artifacts expire after one day. Failed diagnostics retain three days. Hard cancellation may prevent diagnostic publication, and final bundle upload failure fails qualification. No mutable host/production resources, private wrapper or GitHub security settings were changed.

### Integration checkpoint after PR #40 merge — 2026-10-23

The owner squash-merged PR #40. Main is now `21b68e479f0a92e136714a75d251d7dfac920881`; actual-main CI [39956032026](https://github.com/Coxf0rd/fwrouter/actions/runs/39956032026) passed all 16 jobs and its downloaded bundle passed offline verification with `--require-main`. PR #41 has been rebased onto this qualified main, preserving its two implementation commits. Its final source SHA `3ad7bf5bd0dc7108276ab570056aece7160cfeed` passed mandatory PR CI [39953643472](https://github.com/Coxf0rd/fwrouter/actions/runs/39953643472) (16 jobs) and branch-regression L6 [39953670262](https://github.com/Coxf0rd/fwrouter/actions/runs/39953670262) (19 jobs); both bundles passed offline replay. These final source runs used the pre-integration base. The PR-base workflow allowlist is now restored to `main` only; after retargeting, fresh exact-head PR and L6 qualification plus independent review remain required. The branch L6 result remains non-promotional and does not establish current-main/release eligibility. No production or PR #35 action is included.
