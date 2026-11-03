# Stage 6.1 — Single Backend Integration

This report records the final E+F+G integration review. It does not declare a
merge, deployment or production acceptance. The current source-bound PR,
squash/main CI and offline-verification receipts are recorded in the owner-local
[integration receipt](/решения/roadmap/fwrouter/qualifications/stage6_1_integration_2026-11-04.md)
and the [canonical roadmap](/решения/roadmap/fwrouter/ROADMAP.md). Recording the
resulting squash SHA there avoids a second integration PR or an unqualified
post-merge documentation commit. Those owner-local records are not represented
as published Git content.

## Reviewed source

- Repository: `/root/fwrouter-approved-main`; branch `stage6/backend-completion`.
- Current main at review: `f7847a39ed89ea34a4f0a5741f1be5f4e4143354` (PR #77).
  The object is already local; no fetch was required for this review.
- Reviewed implementation/publication HEAD:
  `fde77b76672bdeed2e06d9a941a11c42978b94f6`.
- Complete E+F+G diff against that main: 105 files. This integration-report
  publication adds documentation only; application/test/gate inputs are unchanged.
- Protected pre-existing `knowledge/ROADMAP.md` modification remains outside
  the integration, SHA-256
  `401337a6c5200fc2ded32cb374a04a3cc3ddb609692886bfe8ff85f076b32364`.
  Unpublished canonical Stage 6A decisions and history remain authoritative.

## Complete branch review and dispositions

The Orchestrator reviewed the complete diff, workflow/catalog/acceptance
contracts and package reports. Independent persistence/security review covered
DB admission/initialization, migration retirement, CLI, bootstrap, transfer,
backup/rebuild consumers and installer removal boundaries: **PASSED**, with
**135 disposable targeted tests passed**. Independent lifecycle/API/security
review covered application leases/lifespan, seven scheduler policies,
JobManager, caches, prewarm, authority inventory and diagnostic fixtures:
**PASSED**, with **74 targeted tests passed**. Both reviews found no material
in-scope blocker; each retained an existing Starlette/httpx warning. No new
refactoring or application correction was introduced by integration review.

The [twelve-finding dispositions](../stage6_1_package_g_2026-11-03/FINDING_DISPOSITIONS.md)
remain evidence-based: F02's historical runner is retired by support policy;
F03/F05/F06/F07/F08/F09/F11/F12 are resolved within their documented seams;
F01's deployment/public-auth policy is deferred; F04 and F10 remain partial.
These dispositions are not all-finding erasure or proof of production state.

Canonical `schema.sql` remains schema 27 and byte-identical at
`c48686ecde23308ca397a69a4ea3e67de7e2d86697fd6aee74c40cd9f9bf26c6`.
Fresh initialization is absent-file-only/no-replace; existing supported files
are admitted without reseeding, conversion or repair. Exactly two existing
legacy schema-27 profiles retain their known three missing routing FKs.
Unsupported/pre-27/corrupt files fail closed. WAL, permissions, foreign keys,
transactions, revisions/CAS, native proof, identity/credential ownership,
checkpoints, last-good and recovery keep their established owners.

Worker policies, first ticks, intervals, cancellation and retained live handles
remain independent. The demonstrated prewarm race is corrected by excluding
only the stateful settings workspace from incidental warming; explicit UI/API
status synchronization and its legitimate fingerprint invalidation remain.
The packet probe's missing post-fix retained event is not proof of actor absence
or equal hashes; causal RED/GREEN and full unchanged scenario assertions are
recorded separately in the Package G report.

## Existing exact-HEAD qualification

For reviewed HEAD `fde77b76672bdeed2e06d9a941a11c42978b94f6`:

- Push-fast [41728355944](https://github.com/Coxf0rd/fwrouter/actions/runs/41728355944)
  SUCCESS; full L6 [41728394437/1](https://github.com/Coxf0rd/fwrouter/actions/runs/41728394437)
  SUCCESS, 19/19 jobs, both packet repeats passed.
- Final bundle artifact 13274509130, ZIP/API SHA-256
  `5d7decbab955b352bca37c4fc04da43b8ce7c8279bfcc53b11860a56eec3c4a5`.
  Live artifact availability/digest and job conclusions were rechecked at
  integration entry; the existing official offline verifier was replayed from
  clean exact-source checkout and passed.
- 3,494 unique IDs / 3,540 executions, all PASS; zero selected FAIL/SKIP/BLOCKED/
  NOT RUN; 212 selected of 213 catalogued files, all ten L5 anchors passed.
  Sole omitted file is the existing optional L7 worker-crash scenario.

The new documentation HEAD must receive its own qualification. The existing
`ci:full-acceptance` commit trailer selects mandatory-all PR coverage through
unchanged workflow policy: 212 safe files rather than the affected-only 211.
No workflow, inventory exception, budget or assertion is weakened. Required PR
CI, independent source-bound bundle verification, review/ruleset checks and a
normal squash merge remain required. Resulting main must independently pass
its actual-main CI and exact-main L6 with fresh offline verification; branch
bundles do not substitute for main receipts. Package reports preserve their
historical failures, source identities and accepted gates:
[Package E](../stage6_1_package_e_2026-11-03/REPORT.md),
[Package F](../stage6_1_package_f_2026-11-03/REPORT.md),
[Package G](../stage6_1_package_g_2026-11-03/REPORT.md).

## Accepted residuals and stop boundary

F01 trusted-local/uncredentialed management and deployment/proxy exposure;
F04 coordinator coupling; F10 nonreturning custom-loader/follower waits;
single-process/state-root semantics; exact legacy27 FK limitations; optional
L7; stock-host/systemd/reboot and production/external VPN acceptance remain
explicit boundaries. Any unexplained required failure is blocking, not a flake
waiver. No production SQLite, credentials, services, native runtime or network
state is accessed. No Stage 6A implementation, Docker migration, separate
pre-deploy stage or deployment is included. Stop after verified integration;
controlled deployment is a separate owner task.
