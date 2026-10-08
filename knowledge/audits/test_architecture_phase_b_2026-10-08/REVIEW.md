# Final engineering review — Phase B

Date: 2026-10-08. Reviewed checkpoint: `fd8a3e5`, parent audit `38345d4`. Existing branch: `stage/test-architecture-ci-stabilization`.

## Verdict and scope

**Verdict: Phase B ACCEPTED for its reviewed source/refactoring and bounded local verification scope.** Acceptance of Phase B means test refactoring and its local, bounded safety/quality contracts have been reviewed; it does not mean the application regression suite or CI/release gate is green. Phase C/D/CD are not started. Seven historical failures remain unapproved and block affected promotion.

## Review findings and corrections

1. **Selection gap — confirmed, corrected in this review:** shared test bootstrap/setup changes previously selected infrastructure only. Add explicit shared-path consumer domains; central Core/DB changes must also select dependent selector/watchdog/provider/subscription and connected persistence/runtime/public contracts. Planner tests must assert concrete mandatory domains/files, not only equivalence to its own graph. Native requirements/profile blockers remain visible; no broader application suite is launched on production.
2. **SQLite probe teardown — confirmed, corrected:** a connection context commits/rolls back but does not close SQLite. Backend probe now uses function-scoped temporary state and explicit close; the standalone isolation probe also closes its connection. This changes test cleanup, not the product.
3. **Helper semantics — verified:** AST inspection against `38345d4` confirms 18 state/settings helpers, 9 additional live-probe-cache helpers and 2 scheduler-disable helpers. Corresponding shared helpers preserve statement order and effects; specialized setup remains local. A helper alias does not alter application behavior.
4. **Assertion quality — verified:** persisted module row replaces unconditional PASS; event-controlled concurrency observes the real writer guard; full-row snapshots detect read-side writes. Stable synthetic incarnation is appropriate for tests specifically asserting unchanged-config behavior, while real native readiness/parity remains a separate mandatory gate. No fake result counters or global runtime success were introduced.
5. **Bootstrap — verified within Python scope:** before-app import ordering, dotenv disablement, isolated environment/path setup, socket/process denial and source-to-runtime singleton path retargeting. This is not kernel confinement. Native/extensions/direct syscalls, SQLite ATTACH and arbitrary untrusted tests must not rely on Python hooks.
6. **Teardown — verified within tested scope:** job-manager idle checks and cache reset, idempotent direct-root cleanup, coordinator report consumption before parent cleanup. Explicit keep-artifacts opt-in preserves owned roots only; it grants no runtime/network capability. Unknown/failed cleanup cannot establish acceptance.

## Historical failures

| Count | Disposition | Review basis |
|---|---|---|
| 4 resolved fixture failures | PASS retained | Actual Core drift/apply/readback/fence and synthetic incarnation no-op tripwires; assertions preserved. |
| 5 read-only failures | Confirmed product-contract follow-up | Every affected full-table snapshot differs solely by one lazy `watchdog_state` id=1 insertion. The loader calls ensure/INSERT. No read-only assertion was relaxed and no product code changed. |
| 1 topology discrepancy | Unresolved source/oracle decision | The fake records single reads `Single`, `Unavailable` in addition to batch observation. Determine whether the fallback is required by topology semantics; do not erase the zero-call oracle simply to obtain PASS. |
| 1 Xray lifecycle fixture | Confirmed test defect, migration pending | The fixture substitutes the actual lifecycle callee, cannot perform deletion/recreation and omits the real result's counts. Phase C needs the real joined application pipeline; fabricated counts would remain false-positive. |

The historical cohort was reproduced on the same guarded Phase B fixtures before this review. Those application seams are unchanged by the review's selection/probe fixes; no full historical or full-suite rerun is claimed. Per-ID evidence remains in [BACKEND_REPORT.md](BACKEND_REPORT.md). These categories are not exceptions/allowlists.

## Native/integration compatibility

The current routine unit bootstrap intentionally cannot execute real subprocess/network/native tests. Native and process-dependent plans are blocked, not accepted. This is an explicit Phase C admission dependency, not a working native harness. Compatibility must be supplied by a distinct qualified integration test root/container, without routine global mocks, and without an environment bypass on the host.

The updated [Test Architecture contract](../../PROJECT_MAP/TEST_ARCHITECTURE_AND_CICD_FOUNDATION.md) defines controlled owned subprocesses/private sockets within verified container confinement. No production mounts/secrets/host Docker socket/network/routing are allowed inside the application container; the outer disposable hosted orchestrator owns cleanup. Pinned runtime, bounded resource limits and fail-closed receipts are prerequisites. Real GitHub runner validation still needs separately authorized Phase D/E and push.

## Phase C acceptance prerequisites

The [CI stabilization contract](../../PROJECT_MAP/CI_STABILIZATION_AND_GATED_CD.md) now contains concrete entry/exit criteria: isolation positive/negative tests; full real application Xray CRUD/route/batch; deterministic crash/restart/checkpoint recovery; revision/incarnation/CAS concurrency; last-good/rollback; generated/mounted/native loaded-state parity; Provider/Mihomo negatives; RU/EN browser acceptance at 1440/390; exact failure closure and durable resource/timing/cleanup receipts. No mocked candidate check can replace loaded-state evidence. Unsupported host reboot/systemd scenarios remain explicit unverified release gates.

## Complementary checks

- Guarded backend isolation file: **3 passed**, 0.49 s. SQLite exact node repeated independently: **1 passed** at 0.42 s and **1 passed** at 0.44 s; per-test state and close verified.
- Selection/bootstrap gate contracts: **28 passed**, 0.473 s; standalone isolation smoke **18/18 PASS**, manifest validation **151 suites PASS**. Tests assert named Core consumers and all declared backend consumers for shared bootstrap/DB paths, with required-profile blockers retained.
- L0: changed Python AST, JSON/TOML parsing, whitespace and source-surface checks PASS; committed-range check is repeated after the local commit. No application import outside the guarded child.
- No L6/L7, production integration/native/recovery, runtime calls, provider requests, workflows, deploy/restart or push were performed. Larger application L5/native execution remains blocked by its qualified-profile requirement.

No comparable whole-suite speedup or hosted compatibility is claimed. This review addresses verified test infrastructure defects only; the operational branch remains unchanged at `6004400`.

## Static import cleanup

The helper migration left 27 newly unused settings/cache imports in 23 modules. Removed only imports newly made unused by this refactor; AST comparison against `38345d4` now finds none. Existing test/helper behavior is unchanged.
