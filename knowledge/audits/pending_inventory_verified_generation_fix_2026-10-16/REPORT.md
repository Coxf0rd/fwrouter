# Pending inventory / verified native generation recovery

## Scope and source boundary

This source-only change starts from main `d71e487`. Production remains on `69ec419`; no deployment, restart, native apply, provider refresh, credentials, database or network mutation is authorized by this report. Historical audit evidence is preserved separately. Final test/QA/PR results must be read from the final handoff/PR at its exact head; this report does not predeclare acceptance.

## Root cause

Subscription acquisition/persistence commits normalized server inventory and source memberships before native staging. `subscription_state.status=success` denotes inventory success (including partial-source success with last-good retention), while the outer refresh may later fail before the generation checkpoint exists. Startup previously treated that acquisition status as publication authority. A later startup/general reconcile could therefore regenerate native config from pending inventory without a new explicit refresh. A stale config.next is not the authority: removing it cannot remove persisted intent.

The observed October16 production example changed two VLESS names/Reality short IDs and selector references; original78Xray identities and66last-good files were preserved. Exact upstream intent and staging-context mismatch actor remain UNKNOWN. No production data is used in regression fixtures.

## Admission and recovery contract

Use the existing subscription persistence owner and generic settings table for a versioned internal generation admission marker, without a schema migration. The marker separates pending inventory digest/revision from the last verified digest/revision; it does not replace canonical inventory, the existing generation checkpoint, native observation or last-good.

- Inventory rows and candidate admission update in the same SQLite transaction. A failed marker write rolls back the inventory write. Historical proof is retained only from a coherent marker tuple. A usable exact-integer revision high-water advances even when proof is invalid; an unusable counter stays UNKNOWN rather than restarting a revision and permitting stale-admission ABA.
- Pending inventory remains persisted and visible as pending. Generic native generation cannot silently publish it.
- Explicit refresh/apply carries an internal admission bound to the exact inventory digest/revision; publication still uses existing Core selection/CAS/incarnation and native readback fences.
- Candidate validation is distinct from native verification. A marker becomes verified only after the owning publication path's native proof and public snapshot promotion, before checkpoint closure. No restored file or boolean alone is sufficient.
- Existing checkpoint recovery must bind inventory and re-prove current native state; stale operations cannot adopt newer inventory. Interrupted legitimate apply remains pending if proof is unavailable.
- Missing/malformed legacy markers are UNKNOWN for nonempty subscription inventory; a true clean empty inventory is a distinct case. Never seed verified from current unverified discovery simply to unblock upgrade.
- Equivalent inventory, stale candidate files, explicit source deletion and concurrent refresh/reconcile are separate safety cases. Direct candidate promotion must not use a verified marker for A to admit stale candidate B.
- Optional adapters remain optional; no Xray requirement is introduced for the Mihomo-only refresh path. Existing applied-artifact restoration/readback and independent routing recovery are not a blanket inventory-apply authorization.

## Upgrade limitation

A legacy installation with nonempty subscription inventory and no admission marker may report unverified/blocked generation reconciliation while preserving its active native files. This is a deployment gate, not successful automatic recovery. In particular the current production pending inventory must not be declared verified, force-refreshed, reset, or applied during this source task. A later rollout needs separately approved provenance/admission and complete preflight; this PR alone does not authorize it.

## Test policy and outstanding qualification

Expected-behavior component tests are exercised against immutable base and changed source. Group related edits before affected tests. A qualified synthetic acceptance scenario uses loopback-owned subscription data and real isolated Xray/Mihomo children; no production endpoints, UUIDs, subscriptions or backups are fixtures. It must distinguish pre-checkpoint failure/restart, explicit retry, post-native pre-promotion checkpoint recovery, and stale binding refusal. No destructive production or L7 test is authorized.

Source, component/native tests, independent QA, final full PR CI, user merge, actual main CI, deployment and Live Acceptance remain separate gates. Startup45s and cgroup2GiB limits are unchanged. Do not claim long-soak, full Debian host recovery or production savings from synthetic results.

## Scenario qualification matrix

| Scenario | Required behavior | Current qualification |
| --- | --- | --- |
| Inventory persisted, staging fails before checkpoint | Keep candidate pending; preserve native and last-good; startup cannot apply it | Component coverage implemented; hosted native execution pending |
| Explicit retry of exact candidate | Existing native validation/apply/readback, then verified promotion | Hosted native execution pending |
| Native applied, publication interrupted before marker promotion | Bound checkpoint and fresh native proof can complete publication | Regression implemented; hosted native execution pending |
| Checkpoint inventory revision/digest superseded | Refuse adoption; preserve checkpoint diagnostics and native state | Regression implemented; hosted native execution pending |
| Equivalent inventory refresh | Preserve verified generation without unnecessary recreate | Component coverage; full qualification pending |
| Nonempty legacy inventory without marker | UNKNOWN; preserve active native files, block unproved regeneration | Intentional conservative upgrade limitation |
| Stale manually generated candidate | Reject candidate whose bytes/input provenance differs from current inputs | QA corrective implementation in progress |

## Evidence ledger

The initial nine expected-behavior component cases failed against the immutable base because admission/marker functionality was absent. The initial changed-source component group passed 228 tests. Neither result qualifies the subsequent QA corrections. Independent QA returned CHANGES_REQUIRED for strict marker schema typing, stale manual-candidate provenance and missing post-native checkpoint recovery coverage. Those findings are grouped into one corrective package before rerunning affected checks.

A broader exploratory component invocation reported 293 passed / 36 failed. Its failures include fixture assumptions about newly gated unverified inventory, outdated private-call signatures and native/profile-dependent tests run without their qualified host prerequisites. This invocation is retained as failed evidence, not reclassified as PASS. Applicable fixtures and contracts must be corrected and exercised through their supported profiles before final acceptance.

No native receipt, startup/memory measurement, full required CI, final independent QA or PR readiness is claimed at this checkpoint. Protected raw evidence remains outside Git under `/root/fwrouter-recovery/pending-generation-fix-20261016/`; unrelated audit reports and pending documentation patches are preserved.

### Corrective component checkpoint (not final acceptance)

After the first QA corrections, the grouped component run passed 402 tests; one cross-process writer-lock node was deliberately not selected because the ordinary process profile forbids subprocesses. That node remains required in its existing qualified child-process profile. An additional optional-Xray subset passed 17 tests (overlapping coverage; counts are not additive). Independent QA separately ran 54 component cases successfully.

Independent review then found two further issues: malformed historical marker proof could be carried forward by an upsert, and inventory-admitted staged apply omitted the captured fingerprint check for non-inventory inputs. Both findings are accepted and assigned together for test-first correction. Earlier green counts therefore do not qualify the final source. Native execution and final CI are still NOT RUN.

### Final local corrective checkpoint

Test-first regressions reproduced both independent QA findings: 7 failures / 1 pass before the corrective source changes. The corrected affected group passed 412 tests; one cross-process lock node remains assigned to the qualified child-process profile. The focused generation/staged-candidate subset passed 26 tests. Counts overlap and must not be added. Scoped F821, compilation and diff-check passed. Repeat independent QA and hosted native/required CI remain outstanding at this local checkpoint.

### Independent corrective review

Repeat independent QA: PASSED for the frozen corrected source/diff, with 63 independent component/recovery tests passed and diff-check passed. Historical-proof validation, revision high-water/ABA protection, staged-input revalidation and owned transition semantics were reviewed. This is source/component qualification; real isolated native acceptance, qualified cross-process locking, final required CI and rollout remain separate outstanding gates.
