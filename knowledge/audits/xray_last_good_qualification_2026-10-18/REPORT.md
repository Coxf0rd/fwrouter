# Xray last-good recovery qualification

## Baseline and scope

Baseline main6a5fad205099c7f01fbae15d01c0525d22bfefdc is the single-parent PR23 squash, tree-equivalent to qualified1ff69ff. Main CI39123313392 SUCCESS was checked before branch creation:1818routine+79JUnit=1897PASS/0FAIL/0SKIP, main smoke passed, aggregate eligible, source/plan/profile/native/cleanup receipts bound. No conflicting open PR or active implementation existed at preflight. Existing canonical roadmap and pending documentation patches are preserved; source-only work never authorizes production admission/deployment.

## Root cause and retained expectations

All four original R1 cases failed on the unchanged baseline in the existing isolated exact-node runner. The failure was not caused by unavailable host privilege: real recovery refused the fixture's policy-unproven Mihomo preimage with stale_checkpoint_declined/mihomo_preimage_policy_unproven, before any active restore. The three final-apply cases then failed their last-good Xray bytes assertion; the post-inventory case failed its derived-row restoration assertion. Reproducing failure on an accepted baseline does not waive it.

The original fixtures initialized Xray and durable intent but did not seed a policy-valid Mihomo last-good preimage. Current recovery requires both ownership/fencing and validated fail-closed policy before restoring artifacts. That guard is correct and remains unchanged. The fixture now writes an owned synthetic policy matching its current persisted routing before injecting the next generation failure. Real policy structure validation runs; the component runner's existing native parser/transport doubles remain explicitly synthetic. No production runtime implementation, expected outcome, timeout, resource ceiling or safety guard changed.

## Four exact scenarios

Common prefix: backend/tests/test_xray.py::

| Exact test suffix | Injected failure / expected outcome | Local evidence | Qualified hosted evidence |
|---|---|---|---|
|test_failed_new_generation_restores_last_good_runtime_and_public_snapshot[success]|Final Mihomo candidate apply fails after Xray apply; restore last-good config/bindings/public snapshots, preserve newly admitted durable credential intent, confirm rollback receipt/readback, close checkpoint|Baseline FAIL; corrected PASS|PENDING final exact-HEAD JUnit|
|test_failed_new_generation_restores_last_good_runtime_and_public_snapshot[receipt_failure]|Same apply fault plus rejected rollback UDP receipt; restored bytes are not success, recovery false/restore_failed, checkpoint retained|Baseline FAIL; corrected PASS|PENDING final exact-HEAD JUnit|
|test_failed_new_generation_restores_last_good_runtime_and_public_snapshot[readback_failure]|Same apply fault plus Mihomo readback failure; no receipt attempted, no false recovery success, checkpoint retained restore_failed|Baseline FAIL; corrected PASS|PENDING final exact-HEAD JUnit|
|test_failed_generation_after_inventory_sync_restores_scoped_derived_rows|Materialization fails after inventory synchronization; restore owned Xray derived rows and both override tables without deleting unrelated user intent|Baseline FAIL; corrected PASS|PENDING final exact-HEAD JUnit|

The corrected isolated cohort is10PASS/0FAIL/0SKIP, covering these four plus unsafe/unproven preimage refusal, native-validation requirements, nested DIRECT/cycle rejection, concurrent user override and startup recovery guards. Exact coverage, sanitized JUnit and owned temporary cleanup are required; this is component evidence, not live native proof.

## Why earlier selected CI omitted them

The manifest assigns test_xray.py to qualified-child-process. Routine/L6 orchestration therefore defers that file, while the native profile executes an explicit reviewed list of12 Xray nodes, not the whole file. None of these four IDs was in that list. A green37-node cohort and successfulL6 aggregate could not establish their results. The existing cohort is extended37→41 and Xray12→16 without dropping any original ID. Both execution and aggregation enforce the new exact list; missing, skipped, duplicate and unrelated IDs remain failures. No broad execution permission or limit is added.

## Qualification boundaries

The qualified-child container uses fresh source/test-only SQLite and credentials, nonroot identity, dropped capabilities, read-only root, internal network, bounded executable tmpfs and owned cleanup. FakeRunner component tests require no live Xray or production endpoint. Their inclusion provides trustworthy execution/JUnit/profile provenance, not native readback by itself.

Real native integration is a separate existing ordinary hosted scenario: tests/application_acceptance/test_xray_generation.py::test_xray_reviewed_identity_migration_preserves_credentials_through_refresh_restart_recovery. It runs real worker/generation owner/SQLite and pinned native processes, injects a bounded candidate-integrity fault, verifies fresh Xray/Mihomo readback and last-good rollback, preserves78 synthetic identities/scopes and snapshots, restores derived rows, and checks newer selection fences. Final source/plan/profile/JUnit/cleanup receipts for that exact scenario are mandatory. Component receipt/readback fault branches remain honestly described as doubles; no claim of destructive host/crash coverage follows.

## Remaining qualification and reassessment

Independent QA, final frozen-HEAD mandatory CI and artifact review are pending. R1 remains BLOCKED until all four exact IDs and complementary real-native recovery evidence pass at that same source. Only then may Operational Closure become ACCEPTABLE in its bounded scope and Stage5 READY as a planning prerequisite. Stage5 implementation and production deployment remain separate user decisions. Original closure audit and failed evidence remain historical records.

## Future deployment prerequisites — NOT AUTHORIZED / NOT RUN

After user Squash and Merge, verify exact-main CI; separately authorize read-only production version/schema/inventory/drift assessment and proof-only legacy-generation admission preconditions. Preserve78 originals/durable mapping, additional legitimate identities, protected backups/last-good/recovery compatibility, API/Gateway dependent lifecycle, Core/native parity, Rescue/DNS/Tailscale/host guards. Agree component scope, checkpoints, stop conditions and safe recovery before any installation. Production Live Acceptance follows only a separately authorized controlled deployment. Synthetic CI is not production admission or fresh live proof.
