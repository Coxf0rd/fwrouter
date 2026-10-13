# Phase B — Test Refactoring & Standardization

Date: 2026-10-08. Audited baseline: `38345d4632d8cf585961ea010480c038133e5ca8`. Branch: `stage/test-architecture-ci-stabilization`.

## Executive summary and acceptance boundary

Phase B source refactoring and bounded local checks are delivered for operator review. This is not an all-green application acceptance or a completed CI stabilization milestone. No application source, deployed configuration, production DB/service, provider account, workflow or operational-performance branch was changed. No push, PR, merge, deploy, live runtime test, L6 or L7 was performed. Phase C/D/E require separate authorization.

The routine Python bootstrap now runs before application imports, scrubs inherited configuration/secrets/proxies, disables deployed dotenv loading and schedulers, and creates private per-process/per-suite SQLite/runtime/lock/temp paths. Python audit hooks deny process execution, network/socket access, production-path reads and external writes. This is defense in depth for reviewed trusted Python tests, **not an OS sandbox**: native/direct syscalls and unreviewed code need separately qualified isolation. Native/real-child-process suites are explicitly blocked by the gate until that execution profile exists. Environment opt-in strings cannot authorize them.

## Quality corrections

- Replace the diagnostic `assert True` with a persisted VPN-module desired-state assertion.
- Replace a sleep-based Xray overlap assumption with event-controlled admission around the real writer guard. Verify a competing operation cannot finish while the first owns the guard, and only one reconcile completes.
- Consolidate only equivalent state-directory/cache setup helpers; preserve specialized setup and per-test mutable state.
- Fail when the shared job manager is not idle before setup or after teardown.
- Strengthen read-only DB contracts from table counts to complete row snapshots/deltas. These now expose actual writes instead of missing same-count mutations.
- Retarget default adapter paths to temporary state; routine defaults do not claim successful runtime enforcement/restart/readback. Success simulations must be explicit at a test-local boundary.
- Correct stale bootstrap selector and unchanged-config incarnation fixtures without weakening their persistence/no-restart/no-generation assertions.
- Gate affected selection computes transitive dependencies, rejects malformed metadata, selects shared Core L5 anchors and checks whitespace in the immutable committed base range. A required unsupported process profile remains a blocker, never a green skip.
- Gate pytest suites get separate owned roots and exact-node receipts; shell installer-test failure cleanup is checked with a copied stub only. No actual installer was run.

Scope: 36 backend test modules modified/added, 29 equivalent setup helpers consolidated, plus gate/smoke contracts and three backend support files. These counts do not claim all suites executed.

See [backend evidence](BACKEND_REPORT.md) and [gate evidence](GATES_REPORT.md) for exact files, nodes, commands and limitations.

## Historical failure disposition

The exact 11 historical IDs were run in a temporary hash-pinned Python environment, with production capabilities denied before imports. Initial guarded observation: 0 passed / 11 failed. Final observation: **4 passed / 7 failed**; no blanket allowlist, suppression or fabricated counters.

| Cohort | Count | Classification / disposition |
|---|---:|---|
| Startup selector restoration | 1 PASS | Stale fixture lacked eligibility and mocked the superseded runtime seam; now real Core transition over a stateful fake proves drift repair and persistence. |
| Unchanged Mihomo/runtime logging | 3 PASS | Explicit stable synthetic runtime incarnation restored the intended no-op fixture contract; original negative native/restart/generation assertions retained. |
| Read-only reconcile/state/Xray branches | 5 FAIL | Exact row snapshots show one newly inserted `watchdog_state` row, and no other table delta. `load_watchdog_runtime_state()` calls `ensure_watchdog_runtime_state_row()`. Product follow-up: absent-safe reads or explicit write/startup initialization; application code deliberately unchanged. |
| Topology observation-cost contract | 1 FAIL | Actual `single_calls` contains `Single`, `Unavailable`; expected none. Preserve the assertion; source/oracle resolution belongs to a reviewed follow-up, not operational-branch cherry-picking. |
| Xray VPN-auto lifecycle | 1 FAIL | Fixture replaces the tested reconcile callee with a success dictionary that cannot perform real lifecycle or expose its counts. Do not add fabricated counts; migrate to real joined lifecycle fixture/acceptance in Phase C. |

The historical 52-ID record is preserved. Four current PASS results and 41 historical separate PASS observations are not a single 45-test or full-suite acceptance. Seven remaining IDs still block any affected promotion that selects them.

## Test architecture and fixture standards

The manifest contains 151 suites across 24 domains (149 is retained as the historical foundation count). Primary L0–L7 taxonomy remains unchanged; L5 is connected-domain selection, not duplicate tests. Assertions must prove observable state/effects and negative calls where needed. Mocks replace external boundaries, not the operation under test. Fixtures declare owner/identity, use deterministic synthetic input, reset caches and mutable state, and fail on cleanup/report/dependency loss. No blanket success default may establish native parity, readiness, rollback or convergence.

The minimal standalone isolation smoke is ready for future GitHub-hosted invocation; no new Actions workflow was created. Ubuntu-hosted compatibility, actual remote invocation and qualified native/process execution remain unverified. The component Health smoke uses a real TestClient; the state smoke uses a projection fixture and is explicitly partial L4 evidence.

## Measurements and executed levels

| Check | Result | Timing / resources |
|---|---|---|
| L0 syntax/JSON/TOML/shell/catalog/diff | PASS | Static checks only; no production operations. |
| L1 gate contract | 25 PASS | 0.414 s unittest time; earlier 24-case resource sample: RSS 25,648 KiB, child CPU 0.156 s user / 0.027 s system, 0/408 input/output blocks. |
| L1 smoke contract | 13 PASS | 2.111 s; synthetic children/injected runners only. |
| Isolation bootstrap probe | 18/18 PASS | Fresh child, environment/path/process/network boundaries and cleanup checked. |
| Reviewed backend representative cohort | See backend evidence | Bootstrap, desired persistence, deterministic guard overlap and shared-helper representatives; no full domain-suite claim. |
| Historical exact 11 nodes | 4 PASS / 7 FAIL | Final outer 2.913 s / pytest 2.38 s; peak child RSS 99,748 KiB. |
| Gate-compatible pinned coordinator invocation | 1 PASS | pytest 0.46 s / outer 0.924 s; peak RSS 65,688 KiB; receipt parsed before parent cleanup. |
| Guarded component smoke | 2 route checks PASS, partial L4 | Outer 1.562 s; peak RSS 82,844 KiB; Health real route, state fixture projection, cleanup verified. |

Affected L0/L1 and reviewed component checks were chosen because bootstrap/fixtures/gate contracts changed. Exact failure triage retained red outcomes. L5 selection semantics were tested with mandatory anchors/transitive dependencies; the full selected application L5 cohort was not executed on this production host. No native/integration/recovery test was run merely to satisfy a checklist.

Initial 11-node observation: outer 3.755 s, pytest 3.24 s, peak RSS 130,104 KiB. Final timing is **not a comparable performance improvement claim**: failure paths, fixture behavior and output changed. Proven resource changes are private temp cleanup, removal of shared fixed cache/basetemp paths, deduplicated setup source and removal of the 0.1 s scheduling sleep. CPU/RAM/disk improvements for the full system remain unmeasured; do not populate suite-duration metadata with these small contract samples. No parallelism was added without isolation qualification.

## Remaining blockers / Phase C readiness

1. Seven exact unresolved IDs: five product read-side write defects, one topology contract discrepancy and one overmocked lifecycle fixture. No approved exception exists.
2. Qualified child-process/native isolation is required before mandatory process/native suites can execute. Python hooks alone are insufficient.
3. Full application Xray CRUD/crash/restart/recovery, native loaded-state parity and browser acceptance remain Phase C work; no acceptance is claimed from partial mocks.
4. Actual GitHub-hosted Ubuntu run and locked environment/native provisioning remain Phase D/E gates. A push requires operator authorization.
5. Global fixture behavior now fails closed; unreviewed suites may contain assumptions previously hidden by global fake success. Do not claim they pass without affected execution.
6. Synthetic dotenv format tests need an explicitly reviewed separate fixture contract; routine bootstrap rejects non-example dotenv reads. Stronger OS-level limits and hostile-test isolation remain separate work.

Phase B supplies safe routine bootstrap, explicit fixtures/metadata, strengthened assertions, durable exact failure evidence and a reviewable selection contract for Phase C. Operational Performance Fixes stay PAUSED/BLOCKED at unchanged `6004400`. Stop after this Phase B checkpoint; do not start Phase C/D/CD automatically.

## Final engineering review

Phase B **ACCEPTED in bounded source/local-test scope** after the [final review](REVIEW.md). Fixed incomplete shared bootstrap/Core/DB affected selection, explicit SQLite connection teardown and 27 newly unused imports. Gate contracts 28 PASS, isolation smoke 18/18 PASS, backend isolation file 3 PASS. Four historical fixture fixes remain justified; seven failures remain unapproved. Acceptance permits readiness planning for Phase C, not automatic execution or application release promotion. Qualified native/integration harness and concrete Phase C acceptance criteria are documented; not implemented.
