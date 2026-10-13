# Phase C — Missing Test Coverage

Date: 2026-10-08. Baseline: `f50d7e6`; branch: `stage/test-architecture-ci-stabilization`.

## Verdict

**BLOCKED for acceptance; source harness checkpoint prepared.** Native/application/browser/recovery suites are NOT RUN. This production host is not the authorized environment for them. Source preparation and pure validator tests do not establish application correctness or container runtime isolation. Phase D/E are not started. Operational Performance Fixes remain PAUSED/BLOCKED on unchanged `6004400`.

## Architecture and authority

The outer launcher owns a private Compose project and validates the rendered configuration, image provenance, container mounts/resources/environment and internal network before executing tests. The inner unprivileged container has no host network, published ports, production mounts, Docker socket, credentials or privileged capabilities. SQLite, config, locks and runtime processes belong to its private temporary root. Native binaries and a complete browser bundle require pinned digests; source and UI digests bind receipts to the tested tree. Hosted job layout checks are conservative admission checks, not cryptographic attestation of a fresh VM.

A real HTTP application worker uses the existing API/jobs/services and RealXrayAdapter. Its runner transports operations to actual child Xray processes instead of Docker. Native loaded identities are read through Xray's API; launch-config readback reads the actual owned process configuration. This is native-process application acceptance, not stock Docker transport, host systemd, nftables or end-to-end dataplane acceptance. Startup tasks are disabled; worker restart is not proof of production bootstrap recovery. Synthetic blocked-egress CRUD does not prove private routing or subscription-profile generation.

## Source scenarios and boundaries

Five new hosted application definitions are prepared: three API/native, one real browser, one explicit L7 worker-crash. Fourteen pure acceptance contract definitions and two added gate contract definitions protect source export, profile admission, selection and resource ownership. See [TEST_INVENTORY.csv](TEST_INVENTORY.csv), [COVERAGE_MATRIX.csv](COVERAGE_MATRIX.csv), [infrastructure](INFRASTRUCTURE_REPORT.md) and [application](APPLICATION_REPORT.md) reports, and `tests/acceptance/CONTRACT.md`. The full historical inventory remains in the Phase A report; this delta does not replace it. Ordinary gate execution rejects hosted-only suites. Functional/native/browser and explicit L7 worker SIGKILL selection are separate. No workflow, application code, deploy or live changes are included.

## Local verification

L0 AST parsing passed for 14 Python files; three JSON documents and the product TOML parsed successfully. `git diff --check` and the monorepo surface check passed. L1/L2 pure harness/gate contracts passed 44/44 in 0.474 seconds (0.560 seconds observed tool wall). See [infrastructure evidence](INFRASTRUCTURE_REPORT.md); CPU/RSS were not measured because `/usr/bin/time` is unavailable. Manifest validation reports 155 classified test files. These are affected test-infrastructure checks, not a full application L5 or L6 run. No native binaries, browser, Compose launch, real application acceptance or L6/L7 execution occurred here. Runtime duration/CPU/RSS/I/O and container compatibility remain unmeasured until a qualified hosted run. Synthetic validator PASS is not native acceptance.

## Historical failures

Seven failures remain unresolved: five watchdog_state read paths expose lazy state initialization writes; one topology assertion/oracle discrepancy remains unresolved; one overmocked Xray lifecycle fixture lacks real deletion/counter effects. No assertions were weakened and no baseline allowlist was introduced. The native-process replacement is prepared but NOT RUN, therefore it does not retire the historical fixture failure. Phase B evidence for the four fixed historical IDs remains historical exact-ID evidence; they were not rerun by Phase C.

## Required acceptance before closure

1. Review source and qualify immutable Ubuntu/Python/native/browser inputs on GitHub-hosted infrastructure; prove fail-closed behavior using actual container inspection, not only synthetic objects.
2. Run the functional application/native and browser suites twice from clean state. Require exact selected IDs, nonzero execution, no implicit skips, native loaded-state proof and successful teardown.
3. Extend and run full subscription-profile Xray CRUD, route changes, binding preservation, generation checkpoint recovery, apply/restart/readback faults, revision/incarnation/CAS conflicts and rollback; worker SIGKILL alone does not close these contracts.
4. Add joined Core/Provider/Emergency Direct/Mihomo/migration acceptance: timeout/429/5xx/unknown must produce neither DOWN nor unauthorized mutation; exclusive intent and fixed bindings must survive recovery. Existing component evidence remains valid but does not replace these missing joined paths.
5. Browser acceptance must prove application persistence and rendered error behavior, plus RU/EN/navigation/1440/390. Source preparation is not executed UI evidence.
6. Measure suite duration, CPU/RSS/temp-disk consumption and orphan cleanup. Record unsupported host-kernel/systemd/reboot scenarios as explicit gates.

No Phase D authorization is implied. No return to Operational Performance Fixes or acceptance/merge approval is justified by this checkpoint alone.

## Review findings corrected in source

The review corrected a browser bridge missing PUT request bodies, an API-origin duplication, a stale worker URL, archive decoding, launcher stopped-container lookup, malformed embedded Python preflight, full-tree provenance copying, early profile qualification and cleanup outcome handling. These are defects in the newly prepared harness; no production behavior was changed. Pure contracts check key admission/provenance failures, while actual runtime correctness remains unverified.
