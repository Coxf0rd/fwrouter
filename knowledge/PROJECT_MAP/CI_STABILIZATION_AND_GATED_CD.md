# Test Architecture & CI Stabilization

Status 2026-10-08: **Phase A delivered (`38345d4`); Phase B test-only implementation and bounded isolated verification delivered for review; 4/11 historical failures PASS, 7 unresolved.** Phase C/D/E and remote CI acceptance remain OPEN and require separate operator authorization. Canonical execution authority remains [/решения/roadmap/fwrouter/ROADMAP.md](/решения/roadmap/fwrouter/ROADMAP.md). Foundation `178b471` remains the historical completed Source/Tests/Commit checkpoint. Phase B changes test infrastructure only: no application/runtime/DB/services/provider changes, workflow implementation, push, deploy or CD. Superseded status text is preserved in [Phase B history](../history/TEST_STABILIZATION_PHASE_B_2026-10-08.md).

Operational Performance Fixes are PAUSED/BLOCKED on local branch `stage/operational-performance-fixes` at `6004400`. This CI branch starts at main `e49510c`; it does not contain those optimization commits. Full application staging acceptance is mandatory before operational PR/merge. After CI acceptance, return to the operational branch, integrate current main, and complete staging/correctness/performance acceptance. Package 3 runs only if new evidence proves it necessary.

## Phase A — Comprehensive Test Audit

- Inventory every backend, UI/Node, provider/protocol, native Mihomo/Xray, lifecycle/recovery/concurrency, browser, smoke/deploy and staging/failure test, plus fixtures and mocks.
- For each test record its actual assertion, domain, L0–L7 level, runtime/dependencies, fixture owner, approximate duration, resource cost, isolation, destructive/live capability and the contract it proves. Distinguish white-box, black-box, component, integration, smoke and regression evidence.
- Review assertions against current architecture contracts and user-visible behavior; identify weak assertions, false positives, untested behavior, duplicate coverage and missing failure paths.
- Audit fixture version/schema/runtime assumptions, determinism, secret removal, shared ownership and production independence.
- Reproduce and classify every known baseline failure by exact node ID, commit, environment and root cause. The existing eleven last-failed observations are not an allowlist; do not claim their removal without exact passing evidence.
- Produce a reviewed matrix by test level, domain, criticality, required runtime and gate. Keep historical 149-file catalog count tied to `178b471`; recalculate current inventory rather than reusing that number.

## Phase B — Test Refactoring & Standardization

- Establish consistent test naming, setup/teardown, assertion quality, diagnostics, timeouts, resource bounds and ownership.
- Fix flaky tests, invalid assumptions, fixture drift, incorrect assertions and environment-dependent behavior.
- Simplify and optimize fixtures, setup cost and duplicated coverage while preserving independent contract checks.
- Never adjust an assertion to match a known-bad product behavior. Fix the product or mark a proven environment limitation explicitly; do not create a broad baseline exception.
- Retain exact before/after test inventory and duration/resource evidence for material suite changes.

## Phase C — Missing Test Coverage

Establish and close these coverage gaps against the current main implementation/contracts and isolated native fixtures; do not make Operational Performance Fixes acceptance a prerequisite for this CI milestone. That retained branch is tested as an exact candidate only after CI stabilization is accepted and the branch integrates current main.

Add contract-focused coverage for full-application Xray CRUD and lifecycle; crash/restart/recovery; native loaded-state and applied-state parity; durable persistence; fencing/revisions; concurrency/interleavings; rollback/last-good; and negative outcomes that must not report false success. Cover Core selector ownership, Provider semantics, Mihomo generation/apply/readback and other critical invariants. Use deterministic provider/network/process boundaries and explicitly isolated runtime adapters. Do not run destructive production tests.

## Phase D — GitHub Actions CI

- Use GitHub Actions with **GitHub-hosted runners only**. No self-hosted runner, project-owned VM, KVM/QEMU test VM, Kubernetes, or new CI service. The product deployment stack remains systemd plus Docker Compose.
- Run multiple explicit jobs/stages with increasing cost. Push runs fast L0–L1. Pull requests run affected L0–L5. Main runs required integration and native smoke. L6 is nightly/manual/release-policy only. L7 is a separate release gate only after the required scenario is shown compatible with the isolated GitHub-hosted environment.
- Docker Compose may start disposable integration dependencies within the hosted job. No production mounts, runtime sockets, credentials, routes or state.
- Changed-file selection is deterministic and dependency-complete. Unknown/shared changes expand the gate or fail closed; required tests must never be silently skipped.
- Cache immutable build/dependency inputs with explicit keys and bounds. Reuse build artifacts rather than repeating expensive builds. Bound disk, CPU/RAM, wall time, process count, logs and artifact size; clean only job-owned state.
- At implementation time, verify the repository visibility and current official GitHub-hosted runner limits, job/storage/artifact quotas and available native runtimes. Set budgets from those current facts and observed suite cost; do not hard-code unverified numbers or assume private-repository capacity for this public repository.
- Never expose production secrets or deployment authority to push/PR workflows. CI tests do not contact production or provider accounts.

## Phase E — CI Acceptance

- Run actual GitHub Actions workflows on the intended public repository and retain commit-bound, redacted reports for each required job.
- Prove production isolation, deterministic affected selection, dependency coverage, repeatability and stability; include measured test duration, coverage, CPU/RAM/disk and artifact sizes.
- Resolve audit/refactoring findings and baseline failures with exact evidence. No blanket allowlist or false-green summary.
- Explicitly list scenarios unavailable or unsafe on GitHub-hosted runners. A scenario that cannot run there is not a pass; it remains a blocker or requires a separately approved future gate. Never silently waive it or report L7 as passed. L7 acceptance requires demonstrated disposable isolation and compatibility, not an environment label or dry-run.
- Acceptance closes only after required push/PR/main/nightly/manual gates are verified in real hosted runs.

## Test selection and level policy

Use the existing [Test Architecture & CI/CD Foundation contract](TEST_ARCHITECTURE_AND_CICD_FOUNDATION.md) for canonical levels and evidence rules. Default implementation work is affected L0 plus affected L1/L2 and required L3/L4; L5 applies to shared/domain contracts. Do not run L6 for every change. Run L6 only by milestone/nightly/manual/release policy, and L7 only as a separately qualified staging/release gate. Reports state levels run and why. Baseline failures are tracked by exact ID and remediated; no permanent blanket exceptions.

## Production deployment and future CD boundary

Production deployment remains manual using the documented installer. For backend/UI source changes, the documented deploy command is `/srv/fwrouter/installer/install.sh --deploy --component backend --component ui`; add `--component docs` when documentation is included (for docs-only deployment, select only `docs`). Deploy mode copies selected components and does not restart services. Restart `fwrouter-api.service` explicitly only when a backend deployment requires it; docs-only deployment needs no service restart. See [Install and Deploy](../INSTALL_AND_DEPLOY.md) and [installer behavior](../../installer/README.md).

CD is **not implemented and is deferred until a separate decision**; it is not a prerequisite for Stage 5. If authorized later, define a standalone protected deployment milestone with manual approval, exact merged artifact/SHA, readiness/Health/native parity/smoke and tested rollback protecting Tailscale, rescue and Internet. Do not add automatic production deploy steps to current GitHub Actions.

## Related evidence and preserved wording

- Foundation source/test report: [audit report](../audits/test_architecture_cicd_2026-10-04/REPORT.md); baseline IDs: [status](../audits/test_architecture_cicd_2026-10-04/BASELINE_STATUS.json).
- Exact superseded roadmap and former self-hosted CI contract: [English roadmap snapshot](../history/ROADMAP_PRE_2026-10-08_CI_STABILIZATION.md), [canonical roadmap snapshot](../history/CANONICAL_ROADMAP_PRE_2026-10-08_CI_STABILIZATION.md), [previous CI map](history/CI_STABILIZATION_AND_GATED_CD_PRE_2026-10-08.md). These are historical records, not active requirements.

## Phase B checkpoint — 2026-10-08

See the [Phase B report](../audits/test_architecture_phase_b_2026-10-08/REPORT.md). Source and bounded local tests are delivered; this is not application-wide acceptance. Qualified process/native isolation, full application Xray lifecycle and hosted runs remain open. No automatic transition to Phase C/D is authorized.
