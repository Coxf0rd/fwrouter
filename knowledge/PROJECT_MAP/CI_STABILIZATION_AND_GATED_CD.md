# Test Architecture & CI Stabilization

Status 2026-10-09: **Phase D implementation is authorized and present on `stage/test-architecture-ci-stabilization`; acceptance is BLOCKED.** Run [37934872853](https://github.com/Coxf0rd/fwrouter/actions/runs/37934872853), head `b580b2e`, completed with affected L0–L3 at 1,112 PASS / 129 FAIL, mandatory L4/L5 absent/incomplete, and the 37-case functional cohort at 23 PASS / 14 FAIL. Qualified Mihomo passed 26/26, isolated Docker Xray 1/1, and minimum application Xray readback 1/1; none closes the failed mandatory gates. The seven-ID cohort previously passed 7/7 in run [37904598829](https://github.com/Coxf0rd/fwrouter/actions/runs/37904598829); five passed and two were skipped/uncollected in this run. **Phase E remains OPEN; no production deployment/restart occurred; CD remains deferred.** Current evidence: [checkpoint report](../audits/test_architecture_phase_d_2026-10-09/REPORT.md), [structured receipt](../audits/test_architecture_phase_d_2026-10-09/LAST_FULL_CHECKPOINT.json), [failure analysis](../audits/test_architecture_phase_d_2026-10-09/FUNCTIONAL_FAILURES_37934872853.csv). Canonical execution authority remains [/решения/roadmap/fwrouter/ROADMAP.md](/решения/roadmap/fwrouter/ROADMAP.md).

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

Run 37934872853 retained 37 functional cases (23 PASS / 14 FAIL) and affected results 1,112 PASS / 129 FAIL at L0–L3; mandatory L4/L5 evidence is absent/incomplete. The linked failure CSV groups the 14 results and records underlying member-delay cause as unknown. Host-networking remains a separate NOT RUN boundary: run 37934437529 recorded `Listener fwrouter-tproxy listen err: operation not permitted`, `Listener fwrouter-full-tproxy listen err: operation not permitted`, and `Listener fwrouter-xray-egress-32579334990b listen err: listen tcp 172.18.0.1:53216: bind: cannot assign requested address`. Functional PASS would not establish TPROXY/nonlocal-handoff capability. Dated Phase B/C checkpoints below retain their historical status and are superseded by the current status above.

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

## Concrete Phase C acceptance criteria (source authorized; hosted execution unverified)

1. **Isolation admission first:** implement the separate qualified integration root described in the Test Architecture contract. Negative tests prove no production secrets/mounts/locks/routes/Provider API; positive tests prove owned subprocesses and private sockets work. Missing pinned runtime or confinement receipt fails the mandatory gate.
2. **Full application Xray CRUD:** real API/jobs → intent SQLite → generation/validation → runtime apply → readiness → exact loaded-state readback → CAS persistence/public export. Create/edit/route/enable-disable/delete and bounded batch; preserve ordinary, exclusive/provider and fixed bindings. Verify delete removes native identity and public export, and no success precedes confirmed convergence. Do not mock the generation/lifecycle function under test.
3. **Crash/recovery:** deterministic crash barriers before launch, after apply/before readback, after readback/before persistence and checkpoint completion; owned container/application restart; repeated recovery idempotent; partial/corrupt state, stale source/generation/incarnation and ambiguous outcomes never blind replay or lose last-good. Keep unsupported host reboot/systemd scenarios as explicit gates.
4. **Concurrency/fencing:** competing operations, revision/intent/exclusive change during probes, incarnation change, stale CAS, cancellation and nested writer guard. No stale writer overwrites newer state; Core remains the only global Auto owner. Use barriers/events, not scheduling sleeps.
5. **Rollback/native parity:** failure to generate/validate/apply/readback keeps verified last-good or reports failed/unconfirmed; no blind rollback; candidate/generated/mounted/native identity and binding parity with exact pinned versions. Loaded-state evidence is distinct from candidate syntax validation and live handshake.
6. **Provider/Mihomo negatives:** normal routes make zero provider calls; API unknown is not DOWN; disabled auto-switch performs no discovery/candidate/PATCH; ambiguous mutation is not replayed. Verify protocol per-entry normalization, mixed ordinary sources, unchanged-config no-op, exclusive eligibility and fixed Xray choices.
7. **Browser acceptance:** isolated application and synthetic data, RU/EN at 1440/390, Admin/User/Settings navigation, no JS errors/overflow; configured Auto vs effective eligibility, write-only secret controls, provider logical/legacy grouping and fixed targets. Request counts prove render/tab/locale does not poll provider. Source/VM unit tests are complementary, not browser acceptance.
8. **Failure closure/evidence:** migrate the overmocked lifecycle test without fabricated counts; resolve the topology oracle with source-backed contract; record read-only product failures separately until an authorized app fix. Exact IDs, versions, fixture ownership, repeatability, cleanup receipts and bounded resource/timing evidence are required. No blanket allowlist or hidden skip; unresolved selected failures block promotion.

Phase B acceptance does not grant application release readiness or authorize starting these tests. Remote hosted execution belongs to Phase D/E after explicit push permission.

Phase B final review: [ACCEPTED in source/local-test scope](../audits/test_architecture_phase_b_2026-10-08/REVIEW.md). Seven unapproved failures and qualified native/full-application/hosted gates remain open; Phase C source work has started; Phase D has not started.

## Phase C checkpoint — 2026-10-08

[Missing coverage source report](../audits/test_architecture_phase_c_2026-10-08/REPORT.md): hosted Compose/native-process harness is prepared, with separate functional/browser and explicit L7 worker-crash selection. Actual hosted qualification and joined recovery/fencing coverage remain BLOCKED/NOT RUN. No Phase D workflows, CD, production changes or operational-branch integration occurred. Phase C review is required before proceeding; source-only tests do not unblock Operational Performance Fixes.

### Phase D stopped evidence checkpoint — 2026-10-10

Phase D remains **BLOCKED**. Scoped hosted checkpoints: metadata 9/9 PASS (37963072861), provider singleton 1/1 PASS (37963610399), explicit host-observation fixtures 6/6 PASS (37965069661), eleven-case provider cohort **2 PASS / 9 FAIL** (37965465706). Fresh numeric zero-delay evidence explains only part of the probe failures; recovery/CAS/browser acceptance is still open. Last full results are not superseded by scoped runs. Additional read-model fixtures and fence diagnostics are source-prepared, **NOT RUN**. No Phase E/CD, PR/merge, production deployment or runtime changes. See `knowledge/audits/test_architecture_phase_d_2026-10-09/REPORT.md` and `PROVIDER_COHORT_CHECKPOINT_37965465706.json`.

### Phase D target environment decision — 2026-10-10

The target host remains a standard GitHub-hosted Ubuntu runner. Application/native/browser components reuse the existing single isolated Docker Compose service with Debian 12 userland, pinned Python/native/browser versions and immutable base digest. This is not a Debian VM or host systemd/nftables/TPROXY proof. No production credentials, host network, privileged container or production Docker socket is allowed.

Dependency reuse is ordered after the current native/application blockers: publish a source-free dependency image to GHCR from a trusted publisher, consume an immutable reviewed digest, then build/verify fresh allowlisted FWRouter source for every checked commit. Publishing credentials must be restricted to the publisher job; PR/fork test jobs never receive package-write authority or production secrets. Keep one Compose service unless measurements justify a split. No Docker-image artifact upload as the primary registry. Current GHCR implementation/acceptance is **OPEN**.

Cold measurement in run 37970549010: application image 1,961,517,169 bytes; Compose build 45.328 s; base image absent before build. This is combined pull/build time, not a cache-hit metric. Warm pipeline, immutable dependency image size/download, BuildKit hits, aggregate CPU/RAM/disk peaks and before/after benefit still require measured acceptance. Respect runner resource/disk/artifact budgets; do not label planned caching as an improvement.

Narrow hosted evidence: strict zero-delay fake-only L1 5/5 PASS in 37969825404; explicit read-model fixtures 18/18 PASS in 37971905644. The latter native job was NOT RUN because stale scenario-description inventory correctly blocked preflight. Corrected native/application/browser results are pending. Latest full functional/affected receipts remain unchanged; Phase D BLOCKED, Phase E/CD/operational return not authorized.
