# Proof-only Legacy Inventory Admission

## Problem and scope

PR18 separated discovered subscription inventory from native-verified generations. A nonempty legacy inventory written by older code has no generation marker. The safe generic response remains `unknown / verified_generation_missing`; a backend upgrade must not silently grant publication authority.

This change adds an explicitly operator-initiated admission operation in the existing generation owner. It does not authorize production recovery or deployment. No production database, credentials, subscriptions, runtime, routing, DNS or administrative-access configuration is used as a test fixture or changed by this implementation task.

The design was informed by the local, uncommitted read-only audit at `knowledge/audits/production_inventory_recovery_audit_2026-10-17/REPORT.md`; that private audit is not a dependency of the executable contract and is not automatically published by this PR.

## Safety contract

- A missing marker is legacy state, not proof of corruption or permission to admit.
- Generic startup, refresh preflight, provider re-entry and ordinary reconcile retain PR18 refusal of unknown/unproven generations.
- Explicit admission binds exact canonical inventory and retained credential/publication intent to current source, selection revisions and native runtime incarnations.
- No provider fetch, credential preparation/rotation, native apply, reload, restart or routing mutation is permitted.
- Fresh complete native readback and candidate/active parity are required. Historical success receipts, SQL equality alone and merely running services are insufficient.
- Drift, unavailable capability, incomplete proof, invalid ownership or stale fences return a bounded refusal. They never produce a verified marker.
- The existing inventory owner publishes the pending-to-verified transition. No independent generation manager, database migration or marker-injection shortcut is introduced.
- A dedicated proof-only checkpoint mode must never dispatch into ordinary restore/reload recovery. Explicit repeat may re-prove and close its exact operation; generic recovery safely blocks.

## Owner, CLI and state transitions

The operator interface uses the existing CLI, not a production HTTP acceptance endpoint:

```text
fwrouter xray generation admit-legacy --plan
fwrouter xray generation admit-legacy --expected-inventory-digest <reviewed-digest> --confirm PROOF_ONLY_LEGACY_ADMISSION
```

These are interface examples, not authorization to execute on production. Plan mode inspects existing state without startup, migration, native proof or marker publication. It distinguishes fresh legacy admission, exact owned resume, verified reproof and blocked checkpoints; structural eligibility is never native proof or production GO. Execution returns a closed, redacted result and a nonzero exit status on refusal.

`xray_subscription_service.admit_legacy_subscription_inventory` owns the operation under the existing Xray writer guard. Its private collector builds temporary candidates using retained credentials, requires exact Xray candidate/active/mounted parity and actual loaded users, verifies bindings/client modes and Core selection, then checks Mihomo candidate equivalence and the existing full native UDP/rule/selector proof. Exact Mihomo bytes are checked first; bounded unambiguous semantic comparison is only the fallback for differently formatted files. Missing credentials or capabilities block; no preparation or regeneration occurs.

Proof-only source fences extend the existing generation fingerprint locally to this operation, including public snapshots, Core desired/applied selection, module ownership and independent external connections. Ordinary generation fingerprint semantics remain unchanged. Volatile health observations are not treated as new intent. Canonical Core bypass and the latest apply generation are bound separately; queued/running conflicting Core/apply lock families block entry and final SQLite publication. Their inspection reads the existing jobs table without stale-job cleanup. Attempt-local Core runtime/apply fences must also remain unchanged; active bypass or incomplete Core state is not admitted. An owned latest applied Core generation and matching applied manifest are mandatory. Fresh existing dataplane readback must establish the owned table and actual enforcement mode for DIRECT, selective and VPN; clean SQL health flags alone are insufficient. The final publication fences bind the same Core apply ID, manifest digest and enforcement result. No native proof is performed inside the SQLite write transaction.

State sequence:

1. Exact operator confirmation and digest; reject foreign markers/checkpoints or unowned state.
2. Durable private `proof_only_legacy_admission` checkpoint, phase `proof_started`. Unsafe, unowned or symlinked checkpoint parents are rejected without changing target permissions or files.
3. Fresh native/candidate/source/selection proof and owner-bound Mihomo receipt; phase `proof_complete`.
4. Within an immediate SQLite transaction, revalidate inventory/source/credential/publication fences and create the pending candidate. The existing inventory owner performs verified promotion with additional same-transaction durable CAS and runtime/configuration fences.
5. Record `verified`, unlink checkpoint and fsync its directory. Repeated exact admission re-proves its state without bumping generation revision.

Filesystem checkpoint and SQLite are separate durability boundaries. A crash can leave unknown/pending state with its proof-only checkpoint, or verified state before checkpoint closure. Generic startup/recovery refuses this mode before restore/reload; only explicit rerun may re-prove and finish. Direct restore/current-projection helpers also refuse the mode. Unknown checkpoint modes fail closed. A failed proof can retain a checkpoint and block ordinary refresh until an explicit qualified resolution; it is not an automatic rollback or permission to clear state manually.

## Verification checkpoint before hosted qualification

Test-first RED is preserved: the first isolated regression failed because the new proof collector/entrypoint was absent. Initial local environment and fixture failures are retained as history, not counted as PASS. Corrected synthetic fixtures use actual isolated source/selection fences rather than invented proof values; existing runtime assertions were retained.

Initial grouped targeted verification: **128 PASS / 0 FAIL / 0 SKIP**, exact-node coverage, sanitized JUnit and owned temporary cleanup. Independent QA then returned CHANGES_REQUIRED: Core bypass/apply was outside the fence and plan eligibility ignored foreign checkpoints. New regressions first produced7 FAIL/19 PASS against that candidate; the related corrections were grouped. Corrected targeted verification: **137 PASS / 0 FAIL / 0 SKIP**, exact-node coverage and cleanup PASS. Scope: new proof-only regressions plus subscription native-generation, Xray generation-recovery, CLI generation-diagnostics and subscription refresh-outcome contracts. Compilation and diff whitespace checks passed.

A second independent review confirmed those corrections and identified incomplete Core-native proof outside global DIRECT and unsafe creation through a symlinked checkpoint parent. Six new negative/mode regressions failed against that candidate before the related fixes. The subsequent grouped run exposed nine synthetic proof dictionaries missing the newly required Core-proof field; these fixture failures were corrected without weakening the runtime requirement or negative assertions. Final grouped targeted verification: **148 PASS / 0 FAIL / 0 SKIP**, including all prior 137 nodes plus 11 Core/native-mode, manifest ownership/drift and filesystem negative cases; exact-node coverage and temporary cleanup PASS. Compilation and whitespace checks PASS. A runner-input error used outdated parameter IDs once; no tests ran in that attempt, and only the corrected actual-node run is counted.

The hosted L3 pending-generation scenario is extended with synthetic legacy marker removal, 78 original synthetic identities plus legitimate expansion, real Xray/Mihomo readback, failed proof, API-worker restart, generic recovery refusal, explicit resume and idempotence. Scoped mutation tripwires intercept Xray/Mihomo mutation, concrete dataplane apply/rollback, routing hot-swap and external HTTP, include deliberate interception self-checks, and compare native incarnations/configuration, credentials/public snapshots, last-good and source/provider request counts. Fresh proof receipt writes are checked separately from native artifacts.

A later independent review reran all 148 nodes successfully but found a closed SQLite connection in the real proof collector, masked by unit-level collector substitution. This was treated as a source blocker; a direct collector regression with a real isolated database session failed first and the session lifetime was corrected. A second test exercises the real collector successfully with synthetic native boundaries, without replacing the collector itself. Core native readback occurs after its short database session closes. Final grouped verification: **150 PASS / 0 FAIL / 0 SKIP**, exact-node coverage and temporary cleanup PASS. A denied default temporary path was corrected only in the test harness to use its owned root; isolation guards were retained. Independent repeat QA, real hosted native execution and full mandatory PR CI are still pending at this source checkpoint. No hosted/native PASS or PR readiness is asserted here; final exact-HEAD results will be recorded in the PR and local final handoff without rewriting this pre-run checkpoint.

## Operational boundary

After user merge and actual successful main CI, production use still requires a separately authorized recovery plan with trusted backups, exact digest/source, original credential preservation, no unresolved foreign checkpoint, fresh native proof and independent administrative access. Installing this source or passing hosted tests does not perform admission or qualify the actual legacy production inventory.

If the proof-only operation detects native/configuration drift, stop. A later deliberate native activation/recreate is a distinct authorized operation; do not turn this admission path into a fallback apply mechanism.

## Activation and recovery prerequisites

Do not run a new proof-only checkpoint alongside an old API/timer implementation that does not understand its mode. Before separately authorized production recovery, quiesce legacy writers using the qualified lifecycle procedure or establish that every potential checkpoint consumer runs the compatible source. This source change itself does not stop/restart services. A SQLite CAS can refuse concurrent writes during proof, but cannot retrofit the new checkpoint dispatch semantics into an old running process after a crash.

The actual legacy inventory still needs complete fresh proof and candidate equivalence. The original staging-context mismatch actor, upstream intent, hosted startup margin and historical Chromium SIGSEGV remain unresolved observations; this package does not claim to fix them. Full Debian host/reboot/live rollback, external client E2E and long-soak are separate NOT RUN gates.
