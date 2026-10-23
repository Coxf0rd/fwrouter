# L6 full regression workflow

## Scope and status

L6 is a scheduled or explicitly dispatched full safe-manifest qualification. It is separate from the stable push/PR required check and never deploys. L7 recovery/host acceptance is excluded. The implementation currently being integrated adds parallel execution and a complete, independently replayable evidence bundle; **the current stacked implementation has not yet earned a real exact-head L6 CI PASS**. Source inspection, local tests, a prior run, or branch qualification must not be described as that missing result.

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

Release qualification requires a fresh successful L6 run for the exact current `main` SHA **and** an independent successful actual-main CI run for that same SHA. L6 does not execute L7 or prove Debian host/systemd/reboot parity, production health, external client behavior, or deployment readiness. A branch-mode run cannot substitute for the main run. This workflow has not yet produced a real run of the current stacked implementation, so final L6 qualification and performance claims remain pending until exact-head GitHub execution and offline bundle replay are recorded. No production action is authorized by this document.
