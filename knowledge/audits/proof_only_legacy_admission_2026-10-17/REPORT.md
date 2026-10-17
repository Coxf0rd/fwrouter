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

`xray_subscription_service.admit_legacy_subscription_inventory` owns the operation under the existing Xray writer guard. Its private collector builds temporary candidates using retained credentials, requires Xray candidate/active proof equivalence, exact active/mounted digest parity and actual loaded users, verifies bindings/client modes and Core selection, then checks Mihomo candidate equivalence and the existing full native UDP/rule/selector proof. Exact Mihomo bytes are checked first; bounded unambiguous semantic comparison is only the fallback for differently formatted files. Missing credentials or capabilities block; no preparation or regeneration occurs.

Proof-only source fences extend the existing generation fingerprint locally to this operation, including public snapshots, Core desired/applied selection, module ownership and independent external connections. Ordinary generation fingerprint semantics remain unchanged. Volatile health observations are not treated as new intent. Canonical Core bypass and the latest apply generation are bound separately; queued/running conflicting Core/apply lock families block entry and final SQLite publication. Their inspection reads the existing jobs table without stale-job cleanup. Attempt-local Core runtime/apply fences must also remain unchanged; active bypass, unowned lifecycle, dirty canonical apply state or incomplete native proof is not admitted. The historical `modules.core.runtime_state` observation is not an authoritative applied-generation prerequisite: ordinary apply records its result in `apply_versions`/manifest without necessarily changing that observation. Its value remains bound in the attempt fingerprint; fresh owned manifest and live enforcement proof remain mandatory. An owned latest applied Core generation and matching applied manifest are mandatory. Fresh existing dataplane readback must establish the owned table and actual enforcement mode for DIRECT, selective and VPN; clean SQL health flags alone are insufficient. The final publication fences bind the same Core apply ID, manifest digest and enforcement result. No native proof is performed inside the SQLite write transaction.

State sequence:

1. Exact operator confirmation and digest; reject foreign markers/checkpoints or unowned state.
2. Durable private `proof_only_legacy_admission` checkpoint, phase `proof_started`. Unsafe, unowned or symlinked checkpoint parents are rejected without changing target permissions or files.
3. Collect fresh native/candidate/source/selection proof and record phase `proof_complete`. This phase means proof was collected; it does not mean a receipt or verified marker exists.
4. For an initially unmarked inventory, revalidate inventory/source/credential/publication fences in an immediate SQLite transaction and create the existing owner's pending candidate. Because that marker changes the Mihomo input fingerprint, collect fresh proof again against the committed pending generation. An explicit pending resume also recollects proof; it never reuses a checkpoint's historical proof.
5. Bind the existing admission capability to this exact proof, publish the owner-bound Mihomo receipt, then use the existing inventory owner's verified promotion with same-transaction durable CAS and additional runtime/configuration fences. A receipt alone cannot promote inventory.
6. Record `verified`, unlink the checkpoint and fsync its directory before returning success. An exact verified repeat collects fresh proof and a current receipt, rechecks durable/runtime fences and closes its checkpoint without bumping generation revision.

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

## CI recovery: observed module state versus authoritative Core proof

The initial push exposed a missing test import and an AST/source-catalog docstring mismatch; strict routine aggregation rejected both. Hosted qualification then exposed invalid test-worker accessor imports, corrected with executed callable-path regressions. The next hosted scenario safely blocked before checkpoint creation. Bounded diagnostics established `core_apply_state_unverified`, rather than the test's secondary missing-file error.

The confirmed source cause was a new requirement for historical `modules.core.runtime_state=running`. Schema seeds that observation as `not_configured`; the normal apply-result writer persists `apply_versions` and manifest ownership without changing it. A test using the actual apply-result SQL writer reproduced this before correction. Admission now requires enabled, managed Core, clean canonical apply/routing state and an owned applied generation; full fresh Core native proof is unchanged. Runtime observation remains a concurrent-change fence. Disabled/unmanaged/dirty/missing or stale proof remains blocked.

Grouped targeted result after this correction: **155 PASS / 0 FAIL / 0 SKIP**; gate/acceptance contracts: **214 PASS**. Native collector mode tests also use the historical observation with synthetic boundary doubles. Real hosted qualification and final exact-HEAD full CI remain pending at this documentation checkpoint. Production remains untouched.

## Hosted execution corrections and Xray rule-order proof

The native test fixture intentionally seeds the `vpn` lifecycle owner disabled for ordinary blocked-egress API tests. The proof-only scenario now enables that owner through its public lifecycle after checking the sole saved source is its exact loopback synthetic URL and no enabled provider binding exists. This setup precedes all admission fingerprints/request counters; no refresh is allowed during admission itself. Global ordinary fixture semantics are preserved.

Hosted execution then exposed an incorrect manifest field assumption: the established materializer writes `plan_id` corresponding to `apply_versions.apply_id`. The collector now consumes that actual producer contract; missing, foreign, malformed or conflicting IDs remain blocked. Regression manifests are produced and promoted through the real temporary artifact writers.

Further hosted evidence isolated Xray drift to `routing.rules`: identical sizes/counts and unchanged clients/inbounds/outbounds. An actual synthetic `RealXrayAdapter.stage_subscription_generation` reproduction showed that recovery's subject-order bindings and prospective profile-order bindings can generate the same disjoint per-user rules in different orders. This is not a general license to ignore ordered routing.

Admission uses an exact-byte fast path. Its bounded strict-JSON fallback requires identical typed content outside routing.rules and identical fixed non-user/API anchors at their original positions. Only contiguous segments of exact field rules for a single exact Xray inbound and one globally unambiguous user may reorder; each segment must retain the identical complete rule multiset. No rule may move across an anchor. Duplicate/overlapping, wide or unknown user selectors, changed outbounds/credentials/options/DNS and malformed/ambiguous JSON remain refused. No configuration is rewritten. The actual active/mounted SHA, native incarnation, loaded users/bindings/modes and final source/Core/runtime CAS fences remain independently required.

This narrow proof-only equivalence change requires independent QA and hosted qualification; no final native or mandatory CI PASS is claimed at this pre-run checkpoint. Production remains untouched.


## End-to-end recovery audit — 2026-10-18

The successive hosted failures exposed two different classes of defects. Test fixtures and doubles had invented contracts at several boundaries: the Core observation was assumed to be authoritative applied state, the Mihomo lifecycle owner was incorrectly named, the applied manifest field differed from the actual producer, and whole-collector/receipt substitutions hid real session and input-fingerprint behavior. The corrected integration checks consume real isolated SQLite owner rows, producer artifacts and real worker callables. Synthetic native-boundary tests remain useful for negative paths but are not evidence of a complete native run.

The genuine publication-order defect was that a receipt collected before writing the pending marker no longer matched Mihomo's marker-inclusive input fingerprint. Admission now uses the post-pending fresh proof before receipt publication and promotion. Failures in that second proof retain pending state and the explicit checkpoint; they do not grant verification or trigger reload. Ordinary generation fingerprint semantics and ordinary recovery ownership are unchanged.

Mihomo's builder can also produce different provenance-only sanitation metadata when the previously generated active file is used as its next base. The proof-only comparator allows only the two sanitizer provenance fields to differ, and only after deriving their exact expected values using the existing sanitizer. Runtime and policy fields, unknown fields, actual legacy listener removal, aliases and malformed YAML remain strict. This is separate from ordinary reconcile comparison and never rewrites the active configuration. Xray's narrow disjoint-user-rule equivalence remains bounded by the routing anchors and exact remaining configuration described above.

Diagnostic run 39055595931 and affected run 39055599833 at source `4686d0113c5dacb1bac897cb7bc29271d2858401` both reached `ok=true`, `status=verified` and the expected digest, then failed the same response assertion: `proof_receipt_written` was absent. No other JUnit failure was found in the affected artifacts. This is not yet acceptance of assertions after that point. The grouped candidate makes the success response truthful only after actual successful receipt publication, generation verification/promotion and checkpoint closure; blocked/error responses do not claim it. CLI output retains this field only for exact verified success.

The audit traced entry/ownership, native collectors, configuration comparison, source and runtime fencing, pending/verified commits, receipt consumers, worker/CLI filtering, explicit retry and generic recovery refusal. The qualified hosted case already exercises the real worker, isolated SQLite, real native children, real receipt writer/readback and generation owner in one scenario. Its remaining preservation and idempotence assertions must execute successfully on the final candidate. Supplemental local lifecycle integration is intended to cover real receipt publication/consumption across repeat and checkpoint closure failure without replacing the entire owner with a success mock.

At this audit checkpoint, final hosted end-to-end qualification, independent review of the consolidated candidate and mandatory final-HEAD PR CI are still required. Local targeted results do not substitute for them. Production remains untouched; this report does not authorize or qualify execution against the actual legacy production inventory.


### Admission transition and ownership map

| Transition | Owner and authoritative evidence | Failure / durability boundary |
|---|---|---|
| Entry → eligible legacy or exact resume | `admit_legacy_subscription_inventory`; existing Xray writer guard, operator confirmation, exact inventory digest, source/selection fences, managed lifecycle and owned Core apply | Foreign/malformed checkpoint, existing unowned pending marker, bypass, active conflicting job or UNKNOWN proof blocks; no automatic startup admission |
| Eligible → native proof | `_collect_legacy_inventory_native_proof`; private candidates, Xray HandlerService identities/mounted config, independent Mihomo policy proof and Core applied manifest/live enforcement | No provider fetch, credential preparation or native apply; all required proofs and post-readback fences must match |
| Proof → pending candidate | Existing subscription inventory owner, immediate SQLite transaction with digest/source/selection CAS | Checkpoint persists separately; pending is not verified and cannot authorize generic startup recovery |
| Pending → current receipt | Fresh post-marker collector; `_publish_legacy_admission_mihomo_receipt` → shared owner-bound publisher → existing receipt writer | Receipt is bound to marker-inclusive input fingerprint, active digest and runtime incarnation; proof failure leaves explicit resumable pending state |
| Receipt → verified generation | `_promote_subscription_inventory_generation`; existing receipt consumers and opaque capability, runtime fence, immediate durable CAS | Stale source, selection, apply owner, generation or runtime blocks promotion; receipt alone grants no inventory authority |
| Verified → successful response | Admission owner writes verified checkpoint, unlinks and fsyncs; service/CLI/worker preserve a bounded success contract | Any closure error returns blocked without a successful receipt flag; committed verified state can require explicit fresh reproof to close checkpoint |
| Verified repeat / interrupted closure → completion | Same explicit owner, exact generation, fresh native proof/receipt and durable/runtime checks | No revision bump or credential/native mutation; generic restore/reload remains forbidden for proof-only checkpoint mode |

These are code contracts, not claims that every crash/power-loss or production host scenario has run. Real hosted completion and final CI evidence remain separate gates.
