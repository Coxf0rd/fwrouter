# Test Architecture and CI/CD Foundation

Foundation checkpoint (`178b471`): Source/Tests/Commit completed; current Phase B gate standardization is recorded in [GATES_REPORT.md](../audits/test_architecture_phase_b_2026-10-08/GATES_REPORT.md). The current manifest classifies 162 test files (151 at the Phase B checkpoint). Hosted workflow execution, locked-environment acceptance, native runtime provisioning, deploy authorization/protected deployment, and disposable L7 acceptance remain separate unverified gates. No Phase B edit deploys or restarts production. See the [foundation implementation report](../audits/test_architecture_cicd_2026-10-04/REPORT.md) and Phase A audit for historical details.

Current milestone status: Phase D implementation is separately authorized and has hosted execution. Full run [37934872853](https://github.com/Coxf0rd/fwrouter/actions/runs/37934872853), head `b580b2e`, completed BLOCKED: affected L0–L3 had 1,112 PASS / 129 FAIL with L4/L5 absent/incomplete; the 37-case functional cohort had 23 PASS / 14 FAIL. Qualified Mihomo 26/26, isolated Xray 1/1, and minimum application Xray readback 1/1 passed but do not close those mandatory gates. Earlier singleton run [37934437529](https://github.com/Coxf0rd/fwrouter/actions/runs/37934437529) passed 1/1; prior seven-ID run [37904598829](https://github.com/Coxf0rd/fwrouter/actions/runs/37904598829) passed 7/7 as scoped historical evidence, with five current PASS and two skipped/uncollected. Phase E remains OPEN. No production deployment/restart or CD occurred. See [current checkpoint](../audits/test_architecture_phase_d_2026-10-09/REPORT.md), [structured receipt](../audits/test_architecture_phase_d_2026-10-09/LAST_FULL_CHECKPOINT.json), and [failure analysis](../audits/test_architecture_phase_d_2026-10-09/FUNCTIONAL_FAILURES_37934872853.csv). Dated foundation and Phase C sections below are historical snapshots.

## 2026-10-10 kernel and packet checkpoint

Run [38036768080](https://github.com/Coxf0rd/fwrouter/actions/runs/38036768080),
source `37373a6`, confirms two real Core/native recovery and selector-fence
scenarios: **2 PASS / 0 FAIL / 0 SKIP**. The strict kernel profile has actual
nftables/TPROXY/policy-route preflight and exact native readback. These two
scenarios do not establish LAN packet forwarding or supersede failed broad gates.

The next explicit `packet-diagnostic` L3 scenario reuses the Compose harness,
Debian dependency layers and fresh commit source for LAN/router/local-endpoint
roles on two internal networks. All roles have reviewed `NET_ADMIN`; only the
router gains `NET_RAW` for bounded header-only capture. Profile, namespace,
network membership, routes, egress guards and cleanup must qualify before
application acceptance. See [Network Testbed contract](../../tests/acceptance/NETWORK_TESTBED.md).
Packet source is under review; hosted execution and packet acceptance are
**NOT RUN** at this checkpoint. Full functional, affected, reproducibility,
GHCR publication/consumption and current seven-ID recheck remain open.
Debian host/systemd/reboot/physical-interface parity is a separate release gate;
Phase E/CD remain unstarted and production remains untouched.

## Test levels

| Level | Contract | Typical evidence |
|---|---|---|
| L0 — static | Syntax, import/undefined-name checks, schema/migration compilation, formatting and static policy checks that are already available in the project environment. | Fast, deterministic local checks. |
| L1 — unit / white-box | Implementation-level functions, state transitions, pure transforms and bounded failure semantics. White-box checks may know internal structure and are not by themselves public contract evidence. | Deterministic isolated fixtures; no production state or network. |
| L2 — public/component contract | Black-box assertions against externally observable API, job, event, persistence and component behavior. Keep assertions independent of private implementation details where possible. | Real component boundary where practical; fakes are explicit at external boundaries. |
| L3 — integration | Compose real isolated components across service, temporary SQLite/config/artifact files and runtime adapter boundaries. | Prefer real isolated components; use deterministic fakes only for external provider/network/process boundaries that must be controlled. |
| L4 — smoke | Narrow startup/readiness and one representative public behavior for the changed deployable component. | Disposable or isolated environment; no production mutation. |
| L5 — affected-domain regression | Tests across the changed subsystem and its directly connected contracts. | Explicit affected-test selection plus stable baseline classification. |
| L6 — full suite | Full repository regression gate. | Milestone, nightly, release, broad shared-contract change, or explicitly approved major checkpoint—not every individual fix. |
| L7 — staging failure/recovery | Deliberate failure injection, restart, recovery, rollback and observability checks. | Disposable isolated staging only; never production. |

## Default test selection

The checked-in `tests/gates/manifest.json` gives each test file one primary level (L0 is the independent static gate). File defaults and exact node overrides carry domain, dependency, fixture owner/identity, isolation, network/native/slow/destructive flags, timeout and output/artifact bounds. `tests/gates/gate.py plan` maps immutable changed paths through reviewed domain rules, then selects the transitive dependency closure. Cycles terminate through a visited set; unknown paths and dependency names fail closed. Native suites needed by an affected source path stay mandatory even when their pinned runtime is unavailable; absence blocks the result instead of becoming a pytest skip. Suites that need real child-process execution carry an explicit execution profile. If no qualified profile is available, the gate reports a blocker and does not start the suite or its native preflight; `--include-native` and environment strings do not grant that profile.

L5 is a fixed set of reviewed regression anchors selected for explicitly shared domains, including Core; its tests retain their L1–L4 primary level. Core owns canonical writer, applied-state and public projection boundaries, so Core changes select the shared anchors. L6 is a full-manifest plan available only through explicit manual/nightly/release invocation. L7 has only a dry-run policy until a disposable staging attestation runner exists. A mock, local service, or attestation environment string cannot establish L7.

Do not run L6 after every small edit. For every implementation report, state the levels run and why. Default to affected L0 plus affected L1/L2 and required L3/L4; select L5 when shared/domain contracts change; run L6 only at scheduled/manual, milestone, release, major architecture, or explicitly required policy gates; run L7 only in disposable staging/release acceptance. Expand coverage when a failure, changed contract, broad dependency or release gate justifies it. Do not weaken correctness, recovery, data integrity or resource limits to save test time.

## Isolation, determinism and resource limits

- Tests must not access production SQLite, live dataplane, systemd, production Docker containers, provider accounts, or public networks unless explicitly marked and separately authorized as a live acceptance test.
- Use temporary database/config/artifact paths, unique pytest basetemp directories, deterministic fixtures and bounded fake clocks/processes. Tests that require a native binary use an existing pinned version and isolated synthetic inputs; do not install packages or pull mutable images automatically.
- Keep subprocess/network deadlines, output size, process count, CPU/memory, temporary storage and parallelism bounded. Clean up only test-owned processes, containers and files.
- Manifest rows classify live, destructive, network, native, slow and staging requirements. The plugin refuses live/destructive collection in routine runs; staging remains deny-by-default until a real artifact verifier is implemented.
- Fixture contract: every suite has a named owner and identity in the manifest; mutable state uses unique owned paths and explicit cleanup. Prefer real temporary SQLite/schema behavior for persistence contracts, and mark fakes at external boundaries such as providers, subprocesses or the partial system projection. A missing dependency, fixture, report or cleanup receipt blocks/fails the suite; it cannot become an empty green result.
- The gate runner starts serially, uses a clean child environment with test-owned state/temp paths, disables schedulers, strips inherited FWROUTER/provider/proxy settings, bounds each process deadline and captured output, and terminates the owned process group on timeout.
- L0 whitespace validation checks the immutable committed `base...HEAD` range recorded in the plan, including when the checkout worktree is clean.
- Reproduction instructions state the exact command, repository commit, runtime/tool versions, fixture identity and environment variables that affect selection.
- Keep CPU, RAM, wall time, parallel workers, temporary disk, native process count and container resources bounded. Resource limits are part of test correctness, not only CI cost control.

## Baseline classification and durable evidence

A failure may be called baseline only after reproducing the exact node ID on the declared parent/baseline under a comparable environment. Separate pre-existing failures, environment/fixture failures, skipped tests, fixed baseline failures, novel failures and unclassified failures. CI must stop promotion on any novel or unclassified failure. A known baseline failure may be accepted only through a narrow, explicit and reviewable exception backed by durable classification; do not report raw totals as green. Never silently exclude a raw test failure or broaden a test filter to hide it.

`knowledge/audits/test_architecture_cicd_2026-10-04/BASELINE_STATUS.json` stores all 52 exact historical IDs and triage; it is evidence input, not an allowlist. `approved_baseline_exception` is false. Its historical summary records 41 IDs with later selected PASS evidence and 11 with a retained last failure; that is not one complete post-fix rerun. The Phase B exact-ID follow-up reports PASS evidence for four of those 11 and leaves seven unresolved; this is a narrow 11-ID observation, not a 45-ID single run or a complete suite ([Phase B report](../audits/test_architecture_phase_b_2026-10-08/REPORT.md), [backend detail](../audits/test_architecture_phase_b_2026-10-08/BACKEND_REPORT.md), [per-ID ledger](../audits/test_architecture_phase_a_2026-10-08/BASELINE_FAILURES.csv)). The classifier reports newly failed, unapproved historical, passed-now, skipped/uncollected, retired-with-evidence, and unselected-pending IDs separately. A baseline ID is fixed only when that exact ID executed and passed. Approval exceptions require owner, remediation, evidence, and an unexpired date. Reports bind source commit, plan digest, manifest digest/version, tool version and exact selected suite coverage; promotion eligibility does not itself deploy.

The runner report contains per-suite timing, tool versions, fixture identity hashes, bounded output byte counts and exact node statuses; raw test output and environment values are not retained. The Linux gate and smoke entrypoints create their parent temporary roots explicitly under `/tmp`, independent of inherited `TMPDIR`. Each pytest suite gets a fresh private coordinator root below the gate-owned temporary root, with a validated marker, its own state/cache paths, basetemp and exact-node report. The coordinator removes the outer root only after consuming the report. CI report paths are placed in the hosted runner's temporary directory, then uploaded as commit-named artifacts with a 14-day retention limit. `tests/gates/requirements-ci.txt` pins the Python 3.11 test environment by exact versions and wheel hashes; `backend/pyproject.toml` remains the product dependency contract. Do not retain secrets, provider payloads, production configs, credentials, or raw identity material in test artifacts.

The isolated component smoke command is `python tests/gates/gate.py smoke --profile isolated`; it stages the backend only to a fresh temporary target, creates a canonical-schema DB inside a fresh child-owned state directory, and checks FastAPI TestClient Health/system routes with lifecycle disabled. The child loads the routine isolation bootstrap before application imports. Its state projection is a fixture, so this remains partial component evidence rather than full Core projection acceptance. A separate standard-library isolation bootstrap smoke is prepared for future hosted invocation; it exercises guards in a fresh Python child before application imports and does not claim full application L4 acceptance. Native Mihomo/Xray checks are `not_requested` by default. The explicit `--complete-native --validators-json <file>` option runs local binary/config validation; the standalone smoke path does not verify or enforce the gate runner's `qualified-child-process` profile, and this option does not grant that profile. Use it only as a separately authorized manual check in a disposable isolated environment with pinned binaries and synthetic configs; do not treat it as routine hosted or host-isolated evidence. The routine gate blocks process-dependent suites independently of this smoke option. A live-readonly run requires explicit `--opt-in`, reason, bare loopback API URL, DB path, both native validators with generated/mounted parity, and an exact failed-unit allowlist. No live profile runs in hosted CI.

For a manual single-domain/level cohort, use `python tests/gates/gate.py subset-plan --domain selector --level L1 --output "$RUNNER_TEMP/plan.json"` followed by `python tests/gates/gate.py run --plan "$RUNNER_TEMP/plan.json" --manual-subset --output "$RUNNER_TEMP/report.json"`. A subset report is explicitly non-promotable; it cannot satisfy deploy eligibility. L5 can be inspected with `dry-run --level L5`; L6 requires its own explicit manual plan; L7 remains a non-executing dry-run.

## CI/CD and deploy gates

CI should execute the declared levels in increasing cost and stop promotion on novel or unclassified failures. A known baseline failure may pass only under an explicit, narrow, reviewable policy exception with durable exact-ID evidence; never silently filter it or present raw totals as green. Run L6 at the milestone/release/nightly boundaries. Staging L7 artifacts and reports are separate from production deployment. Deployment must consume a reviewed immutable source commit, run the project installer/checks, and keep Source, Tests, Commit, Deploy and Live verification as separate records. No workflow may auto-run destructive production tests, provider mutations, runtime restarts, schema rollback or broad cleanup as a test side effect.

`.github/workflows/test-gates.yml` defines read-only hosted PR/push checks for L0 and deterministic affected selection. `.github/workflows/test-full-suite.yml` restricts L6 to a scheduled or explicit manual run. Both pin Actions by full commit SHA, use hosted runners, grant only `contents: read`, and have no production secrets or deployment steps. The workflows are source definitions only; no remote activation or successful hosted run is claimed. L7 staging and deploy eligibility consumption remain separate gates. Full operational test/CI documentation is deferred to the Production Documentation stage.


### Later CI stabilization plan — 2026-10-08

The Source/Tests/Commit foundation at `178b471` is a closed checkpoint and is not reopened. The next active milestone is the separately planned **Test Architecture & CI Stabilization**, with audit, refactoring, missing coverage, hosted Actions and real-run acceptance phases. Its current contract is [CI_STABILIZATION_AND_GATED_CD.md](CI_STABILIZATION_AND_GATED_CD.md). GitHub-hosted runners only; no self-hosted runner or project-owned test VM. Existing Actions files do not prove remote CI acceptance. Operational Performance Fixes are PAUSED/BLOCKED until full application staging acceptance; their retained local branch is documented in the canonical roadmap. CD is a separate future milestone deferred until a new decision; production continues to use the manual installer. External telemetry/metrics and traffic accounting remain late after extraction and DB/architecture stabilization.

## Qualified integration/native execution boundary — Phase B review

Routine `backend/tests/conftest.py` is deliberately Python-only and fail-closed. It is not the harness for native application acceptance: its process/socket guard and default adapter fakes cannot establish integration parity. No environment label, pytest marker, binary path or CI detection may weaken it on a production host.

Before Phase C runs real subprocess/network tests, supply a separate integration test root and bootstrap in an independently isolated job/container. Keep unit fixtures out of that root; import the actual application only after isolation/path configuration. The outer hosted harness owns disposable containers, while the test/application container has no host Docker socket, host network, production bind mounts, credentials or production routes. Use synthetic identities, private internal Compose network (no outbound provider access), explicit ephemeral runtime endpoints, job-owned state/config/locks and pinned native binaries/images. Permit subprocesses and sockets **within that confinement**, with bounded time/output/process/CPU/RAM/disk limits and owned-process cleanup. Docker orchestration authority must not be passed into the application container.

The qualified profile must verify its environment and bind its receipt to the exact suite/source/image/config inputs; an unsupported or unverified profile blocks required suites. Phase B defined this contract; Phase C now prepares its implementation, but actual harness acceptance remains NOT RUN. Phase D may wire qualified execution into Actions only after source review. Pure host-systemd/reboot/kernel scenarios unavailable in the hosted environment remain explicitly unverified L7 gates, not substituted with mocked PASS.

Routine production capability-denial tests must continue passing alongside positive qualified tests demonstrating real owned subprocess execution, private service communication, native loaded-state readback and complete teardown. Never solve compatibility by an unrestricted `ALLOW_SUBPROCESS` or `CI=true` bypass.

## Phase C source completion — 2026-10-09

Status: **SOURCE COMPLETE / EXECUTION PENDING**. Checkpoint `c25ef4c` is preserved
in the linked historical report. The hosted native-process harness now has 31
application definitions / 48 exact cases: 37 functional L3 and 11 explicit L7.
Real Xray profile generation, eight durable checkpoint crash stages, CAS after
native readback, newer intent/source rows, joined Core/provider/Mihomo recovery,
SQLite and four real Chromium scenarios are registered. See the
[report](../audits/test_architecture_phase_c_2026-10-08/REPORT.md) and
[traceability](../audits/test_architecture_phase_c_2026-10-08/TRACEABILITY_MATRIX.csv).

`tests/acceptance/scenarios.json` plus source-only AST catalog bind exact IDs to
receipts. Stale/incomplete registries fail closed. Shared application changes
require functional hosted acceptance in the affected plan; the ordinary local
gate still refuses execution. UI requires browser acceptance. L7 never enters
normal affected selection and requires separate explicit recovery authorization.

60 pure local gate/isolation/smoke-contract checks PASS; this is not native L4
smoke. Actual native/application/browser/crash cases are **NOT RUN — pending
Phase D**. Source completion is not full acceptance or authorization to implement
Phase D. Production app/runtime/workflows and Operational `6004400` are unchanged.
Seven historical failures remain OPEN until actual corrected/replacement evidence.

Native process transport does not prove stock Docker/systemd/nft/kernel/reboot
or production startup parity. Test-only fault seams wrap original fsync/CAS and
real external transport; no synthetic successful service/native proof. CAS fault
injection advances the owned real SQLite revision, distinct from public-API
competing intent tests. Desired pending bytes may differ from loaded last-good
on failed reload; do not assert false convergence or discard persistent intent.

Run/dependency/receipt rules:
[hosted contract](/srv/fwrouter/tests/acceptance/RUNNING_ON_GITHUB_HOSTED.md).
No self-hosted runner/project VM, production secrets, CD or automatic deployment.

### Phase D stopped evidence checkpoint — 2026-10-10

Phase D remains **BLOCKED**. Scoped hosted checkpoints: metadata 9/9 PASS (37963072861), provider singleton 1/1 PASS (37963610399), explicit host-observation fixtures 6/6 PASS (37965069661), eleven-case provider cohort **2 PASS / 9 FAIL** (37965465706). Fresh numeric zero-delay evidence explains only part of the probe failures; recovery/CAS/browser acceptance is still open. Last full results are not superseded by scoped runs. Additional read-model fixtures and fence diagnostics are source-prepared, **NOT RUN**. No Phase E/CD, PR/merge, production deployment or runtime changes. See `knowledge/audits/test_architecture_phase_d_2026-10-09/REPORT.md` and `PROVIDER_COHORT_CHECKPOINT_37965465706.json`.

### Phase D target environment decision — 2026-10-10

The target host remains a standard GitHub-hosted Ubuntu runner. Application/native/browser components reuse the existing single isolated Docker Compose service with Debian 12 userland, pinned Python/native/browser versions and immutable base digest. This is not a Debian VM or host systemd/nftables/TPROXY proof. No production credentials, host network, privileged container or production Docker socket is allowed.

Dependency reuse is ordered after the current native/application blockers: publish a source-free dependency image to GHCR from a trusted publisher, consume an immutable reviewed digest, then build/verify fresh allowlisted FWRouter source for every checked commit. Publishing credentials must be restricted to the publisher job; PR/fork test jobs never receive package-write authority or production secrets. Keep one Compose service unless measurements justify a split. No Docker-image artifact upload as the primary registry. Current GHCR implementation/acceptance is **OPEN**.

Cold measurement in run 37970549010: application image 1,961,517,169 bytes; Compose build 45.328 s; base image absent before build. This is combined pull/build time, not a cache-hit metric. Warm pipeline, immutable dependency image size/download, BuildKit hits, aggregate CPU/RAM/disk peaks and before/after benefit still require measured acceptance. Respect runner resource/disk/artifact budgets; do not label planned caching as an improvement.

Narrow hosted evidence: strict zero-delay fake-only L1 5/5 PASS in 37969825404; explicit read-model fixtures 18/18 PASS in 37971905644. The latter native job was NOT RUN because stale scenario-description inventory correctly blocked preflight. Corrected native/application/browser results are pending. Latest full functional/affected receipts remain unchanged; Phase D BLOCKED, Phase E/CD/operational return not authorized.
