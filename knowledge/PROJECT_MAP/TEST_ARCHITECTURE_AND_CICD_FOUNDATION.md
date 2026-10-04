# Test Architecture and CI/CD Foundation

Status as of 2026-10-04: this document defines the proposed test-level and evidence contract. The CI workflows, test markers/selection automation, resource limits, protected deployment gates, and staging implementation are not yet complete. This document is not evidence that those systems exist or run.

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

For an ordinary change, run L0 and the affected L1/L2 tests. Add L3/L4 when the changed path crosses persistence, process, HTTP, generated configuration or runtime boundaries. Run L5 for shared lifecycle, persistence, selector, protocol, provider or API contracts. Schedule L6 for milestone/release/nightly and major shared-contract changes. L7 is a separate staging qualification gate; unit fixtures or mocks do not count as staging acceptance.

Do not run L6 after every small edit. Expand coverage when a failure, changed contract, broad dependency or release gate justifies it. Do not weaken correctness, recovery, data integrity or resource limits to save test time.

## Isolation, determinism and resource limits

- Tests must not access production SQLite, live dataplane, systemd, production Docker containers, provider accounts, or public networks unless explicitly marked and separately authorized as a live acceptance test.
- Use temporary database/config/artifact paths, unique pytest basetemp directories, deterministic fixtures and bounded fake clocks/processes. Tests that require a native binary use an existing pinned version and isolated synthetic inputs; do not install packages or pull mutable images automatically.
- Keep subprocess/network deadlines, output size, process count, CPU/memory, temporary storage and parallelism bounded. Clean up only test-owned processes, containers and files.
- Mark live, destructive, network, native, slow and staging tests distinctly so routine affected-test selection cannot invoke them accidentally.
- Reproduction instructions state the exact command, repository commit, runtime/tool versions, fixture identity and environment variables that affect selection.
- Keep CPU, RAM, wall time, parallel workers, temporary disk, native process count and container resources bounded. Resource limits are part of test correctness, not only CI cost control.

## Baseline classification and durable evidence

A failure may be called baseline only after reproducing the exact node ID on the declared parent/baseline under a comparable environment. Separate pre-existing failures, environment/fixture failures, skipped tests, fixed baseline failures, novel failures and unclassified failures. CI must stop promotion on any novel or unclassified failure. A known baseline failure may be accepted only through a narrow, explicit and reviewable exception backed by durable classification; do not report raw totals as green. Never silently exclude a raw test failure or broaden a test filter to hide it.

Retain a concise, redacted result report in `knowledge/audits/` or a CI artifact with an explicit retention policy. Include source commit, baseline commit, command and markers, environment/tool versions, totals, failing node IDs or a durable reference to them, and exact baseline/novel comparison. A temporary `/tmp` log or JSON may support an investigation but is not durable evidence by itself. Do not retain secrets, provider payloads, production configs, credentials, or raw identity material in test artifacts.

## CI/CD and deploy gates

CI should execute the declared levels in increasing cost and stop promotion on novel or unclassified failures. A known baseline failure may pass only under an explicit, narrow, reviewable policy exception with durable exact-ID evidence; never silently filter it or present raw totals as green. Run L6 at the milestone/release/nightly boundaries. Staging L7 artifacts and reports are separate from production deployment. Deployment must consume a reviewed immutable source commit, run the project installer/checks, and keep Source, Tests, Commit, Deploy and Live verification as separate records. No workflow may auto-run destructive production tests, provider mutations, runtime restarts, schema rollback or broad cleanup as a test side effect.

The concrete workflow files, marker registry, affected-test mapping, durable artifact retention, staging environment and protected deploy job remain implementation work under the current roadmap. Full operational and implementation documentation for Test Architecture/CI/CD is deferred to Production Documentation (roadmap step 17). Until workflows and gates are implemented and verified, this document is a contract only.
