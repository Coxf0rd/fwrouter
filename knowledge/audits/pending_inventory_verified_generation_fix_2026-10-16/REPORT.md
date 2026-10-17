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

### Hosted affected qualification and corrective checkpoint — October 17

Affected runs are diagnostic history, not final acceptance. Run38928181456 at b161b0f failed three routine fixtures (1562 passed); their signatures/native-verification expectations were updated without changing runtime safety. Run38930338359 at b67195e passed1565 routine,37 qualified child-process and1 qualified Xray node but failed four application cases. It exposed an outer subscription-route writer guard that prevented the public refresh selector from entering its own guarded publication path; the route now delegates locking to the existing persistence/apply owners. The grouped route correction passed244 local tests and independent source review.

Run38933610377 at1bf7dfa still failed four application cases. Three strict publication helpers exposed a source defect: staged pending_publication omitted the exact opaque inventory admission, so native work could succeed while the verified marker remained pending. The helper correctly refused to report generation verification. The new lifecycle fixture also attempted VPN-auto probing of deliberately unreachable synthetic upstreams; its supported public-API intent must explicitly select DIRECT and fixed mode without a target, with actual job/readback assertions. No fake upstream success, marker seeding, weaker native proof or timeout/resource-limit change is permitted. These corrections and repeat independent review are in progress; hosted native and full required acceptance remain unqualified.

Fast runs38930329676 and38933602333 succeeded. No final full mandatory PR CI has been requested yet. PR18 remains Draft. Production has not been changed by this task.

The staged-publication regression initially exposed a unit-fixture defect: a receipt publisher stub returned success without a durable receipt. Independent QA correctly returned CHANGES_REQUIRED (1 pass/1 failure); the fixture was corrected to exercise the real durable publication contract. Against pre-correction source the test then reached successful native publication but lacked inventory_generation_promotion (RED), whereas the exact-admission propagation passed both promotion and stale-admission tests (2 PASS). A local test_xray run reported171 PASS/1 FAIL: the failure was the cross-process test rejected by the ordinary subprocess bootstrap guard, not qualified locally. Its hosted qualified execution remains mandatory. Repeat QA and hosted qualification are pending.

Repeat independent QA PASSED for the frozen three-file corrective source/component diff9fbb419aab3e57a06b429332fc4af58f4be93dad0b7fdf55a9057367f994071a:2 focused tests passed, diff-check passed; native/hosted qualification remains outstanding. Acceptance full-file SHA2561dabbf19816dbfda5a5c2b915b2f17c08ca83f8fbba91e72faec0cd85e874ba1 differs from its diff hashb9e67962e1d4043f7b58ab898935cc005353d6ca3fc67188ded7a15313d6acc2 by hash scope, not source drift.

### Explicit migration supersession and qualified profile correction

Affected38938544723 at73c341a passed provider11/11, packet2/2, qualified child37/37 and qualified Xray1/1, but failed two ordinary cases. The lifecycle fixture's real public routing jobs require nft/NET_ADMIN, deliberately absent from the ordinary profile. That exact scenario is now assigned to the existing hosted-kernel-dataplane cohort; identity migration remains ordinary. Strict disjoint/exhaustive selection is27 ordinary +12 provider +2 packet, still39 functional +2 packet with no omitted nodes or additional job.

The reviewed identity migration's explicit supersede operation validated exact checkpoint SHA, migration plan/ledger and baseline, but omitted claiming pending inventory admission before its fresh native generation. Only that validated supersession path now claims the existing opaque capability under the owning writer lock. Generic startup/reconcile cannot claim it; wrong SHA, stale fences and unknown markers remain blocked. Regression first failed with SUBSCRIPTION_INVENTORY_GENERATION_UNVERIFIED; corrected focused tests passed3 cases including generic refusal, wrong-SHA zero claims/native reloads and exact supersession promotion. Profile contract tests passed4 cases after their expected RED. Manifest, compile and diff checks passed. Independent review and hosted qualification remain pending for this correction.

Independent QA PASSED the frozen eight-file correction diffbbc66dd110577d269ff3102008e20d016b1de7e3d48e2eda884adf83917358cf:3 focused component tests,3 independent partition/manifest tests and diff-check passed. Exact checkpoint/plan ledger ownership, baseline revalidation, guarded admission and exhaustive profile membership were reviewed. Hosted native/required CI remains a separate outstanding gate.

### Actual reviewed migration transaction qualification

Root final review found that the preceding supersession claim correction was insufficient for the real migration path: reviewed migration clears identity-migration metadata and restores provenance in raw_json, which belongs to the exact inventory digest, without updating its candidate marker. Writer independently confirmed UNKNOWN/inventory_digest_mismatch. Affected run38943160369 at02b93ab was cancelled on this confirmed defect, not accepted or blindly retried. Prior focused supersession QA did not qualify this untested actual-migration mutation.

The actual reviewed migration now records an inventory candidate through the existing owner in the same transaction when successful apply changes raw bytes. It remains pending, never verified by migration. Dry-run does not persist marker changes; exact unchanged replay does not advance the candidate. Failure to write marker rolls back raw data, credentials, transition ledger and marker together. Forced pending clears a conflicting same-digest historical proof tuple; distinct older proof is retained, with existing high-water/invalid-marker rules unchanged. Regression covered the real dry-run/apply path and rollback rather than a seeded ledger alone. Six focused cases passed; wider affected group215 PASS with one local cross-process bootstrap denial requiring hosted qualification (not PASS). Repeat independent QA and hosted verification remain pending.

Independent QA PASSED the frozen four-file migration correction diff762ccb39553e2e075f38c5bf90c0d0a963457eb4eb7847e12509738c58ef3289:6 selected executions and diff-check passed. Local tests exercise actual migration transaction and explicit supersession separately; the existing hosted reviewed-identity scenario must still prove the single migration→persisted-ledger→CLI supersession→native publication path. No local result substitutes for that gate.

Affected38944509336 at7a35913 failed the routine gate-contract aggregate: one missed old assertion expected28/11/2 instead of27/12/2. Hosted native suites were NOT RUN, not PASS. The one-line expectation was corrected; both complete acceptance/gate contract files passed200 tests in a sanitized loopback-enabled environment (four existing browser-marker warnings). All active partition references were checked; no scenario/receipt requirement changed. Root reviewed the one-line correction.

### Acceptance-owned probes and explicit refresh publication

Affected38945678519 at2c6227d failed two application cases. Actual reviewed migration→CLI supersession successfully preserved78 identities and proved archive/ledger/native state, but a later raw synthetic refresh was followed by generic reconcile, correctly rejected while pending. Three successful post-refresh paths now use the strict explicit publisher when pending; the intentional candidate-integrity fault uses it too, retaining every last-good/native recovery assertion and requiring verification false on failed apply.

The new lifecycle scenario passed public routing setup and verified initial import, then its global fallback/Google-URL filter found no group: the acceptance worker deliberately rewrites probe URLs to profile-owned endpoints. The corrected test selects its exact logical runtime name from its isolated synthetic database and checks fallback plus the exact owned probe URL, retaining2→3 membership and Reality short-ID assertions. No speculative preference or production change is made.

The existing strict publisher now exports only allowlisted actual recovery outcome/boolean proof fields; it does not fabricate success or expose raw configurations, paths, credentials or arbitrary diagnostics. New redaction/malformed/false-proof contract passed. Writer's complete acceptance contract file129 PASS; independent QA PASSED the frozen three-file diff0af79a34dabc00d9d787b63dd514f6744f3219dcde1e8eb3a3f00b73a503be45 and the new contract. Its broader isolated invocation128 PASS/1 loopback-unavailable failure is retained (not PASS). Hosted native qualification and final full CI remain outstanding.

### Reviewed Reality identity change in the native lifecycle scenario

Affected38950603003 at1beebf5 passed all27 ordinary cases, including the complete reviewed migration/restart/recovery/revocation scenario, the other11 provider cases,2 packet cases,37 qualified child and1 qualified Xray. The sole remaining lifecycle failure was IDENTITY_MIGRATION_REQUIRED on retry: changing Reality short-ID intentionally does not qualify as automatic compatible profile growth. The source guard is correct and unchanged.

The synthetic lifecycle case now preserves failed staging/restart refusal assertions, proves no checkpoint exists at that pre-native window, and uses the existing protected CLI to dry-run/apply exact A→B identity continuity with selection/identity/revision fences and reviewed plan SHA. Review alone must preserve native bytes/reload count/all78 baseline identities and leave generation pending. The same public source retry must still establish actual native proof. Later crash-window/stale-binding candidates use strict ordered additions retaining B's exact existing outbounds, separating generation recovery from another credential-policy rotation. Independent QA PASSED the frozen test diff3ea9dcda9cd3297aafe556838b57ea863f980ece619af76d2eeaac9414576611; parser test and diff-check passed. Hosted lifecycle qualification and final required CI remain outstanding.

### Exact injected boundary verification

Affected38954008377 ata34f76d still failed only the new lifecycle case: its first B import stopped at the identity guard before reaching the armed staging hook; checking only ok=false had not proved the intended injected failure. The unconsumed one-shot then caused the later retry to fail. The scenario now separately asserts an unarmed identity-guard refusal, performs reviewed migration, then arms and requires exact ACCEPTANCE_PRE_NATIVE_STAGE_FAILURE plus a consumed marker and no checkpoint, before restart/refusal/retry. Same-source ownership, native bytes/incarnation/reloads/last-good and78 identity assertions are preserved.

Root/QA caught incorrect DTO paths in the corrective candidate before another hosted run (CHANGES_REQUIRED); they were fixed against the actual route envelope. Two parameterized real route DTO cases pass independently; grouped affected route tests4 PASS, scoped F821/compile/diff-check PASS. Repeat independent QA PASSED frozen two-file difff822b1ea0beaf8da704f2595d588811a9d8beda2d1cae3efd2589e5bc6ebe2e5 with2 independent route tests. Native qualification remains outstanding and earlier failures are not acceptance.

### Strict checkpoint-bound DIRECT recovery

Affected38957997037 at98722bb proved the exact pre-native staging fault, pending persistence, restart refusal, explicit reviewed retry, actual short-ID/group delta and78 identities. Its remaining failure occurred after C native apply/snapshots_published: current-projection checkpoint recovery returned preapply_revalidation_failed because the shared selection verifier requires a selected VPN target even for legitimate global DIRECT with fixed/no target. The normal VPN-target verifier remains unchanged.

Recovery now has a separate strict unselected-DIRECT proof: captured checkpoint intent must contain DIRECT desired/applied mode and clean state; current durable routing and selection revision/decision must match. Legacy missing intent is not inferred from current state. A legitimate null decision is accepted only when explicitly present and equal across checkpoint/current/durable fences and no active/provenance VPN server ID exists; wrong/unknown intent, emergency desired VPN, stale fences, native drift or missing proof fail closed. Existing uncached dataplane/owned-table/applied-artifact and fresh classify/enforcement readbacks must prove global DIRECT with guaranteed traffic and matching intent around reload/publication. Existing source/projection/hash/incarnation/bindings/native proof checks remain required.

Test-first regressions covered absent helper and null-decision rejection. Repeat independent QA PASSED frozen source/test diff594ea729f695e4958947e6b4c055cc374e08fedd0e49db46598bb131e91503a6; focused10 and complete recovery39 tests passed independently (overlap, not additive). Writer additionally reports related refresh/VPN25 and rollback-selection2 tests passed. Scoped F821/compile/diff-check passed. Hosted native/required CI still outstanding; no production mutation.

### Remaining packet recovery diagnosis

Affected38961443558 at5071c2d passed all12 provider cases including the full new lifecycle, all27 ordinary cases including identity migration/recovery,37 qualified child and1 qualified Xray. Packet cohort had1 PASS/1 FAIL: verified Emergency Direct→VPN re-entry and packet traffic checks succeeded, then ordinary pending-candidate publication returned XRAY_GENERATION_RECOVERY_FAILED. Existing logs show stale_checkpoint_declined but omit its exact reason; root cause remains UNKNOWN rather than a claimed flaky test/source regression.

A test-only diagnostic package exposes fixed allowlisted restore/recovery reasons, selection proof codes and checkpoint phase/fence/inventory-match booleans. It reads only the suite-owned bounded regular checkpoint with ownership/symlink/size checks; no raw credentials, IDs, hashes, paths or config are exported, and original recovery outcomes/exceptions are preserved. QA initially required five omitted fixed reason codes; correction and arbitrary-string filtering passed. Writer full affected contract132 PASS; independent QA focused4 PASS and frozen diagnostic diff7bc8717b0d55c6126cac601978c0fd82dedf8a9c281652f54d4502f8677a51c9 PASSED. Scoped diff/compile checks passed. Next affected execution is diagnostic, not a blind rerun or final full mandatory CI. Production unchanged.

### Checkpoint creation/publication trace qualification

Affected38982426601 atcf60e30 reproduced the sole packet failure. Restore declined the old checkpoint with selection_fence_changed; current-projection recovery declined inventory_generation_not_bound. Neither refusal is bypassed. Static tracing confirms provider re-entry does not itself create an Xray generation checkpoint; the exact earlier creation/publication operation remains UNKNOWN because prior artifacts lack its lifecycle events.

A packet-worker-only observer now captures bounded closed-enum owner/phase/result and checkpoint existence around real reconcile, creation, publication and restore. It changes no runtime contracts or outcomes, emits no raw paths/configuration/credentials, and caps output at48 records of1024 bytes. First independent QA required symlink-safe checkpoint attribution; the corrected reader rejects symlinks, checks original-file ownership/type/size and inode identity, and uses O_NOFOLLOW. Original publication input is passed through unchanged. Full acceptance contract133 PASS; scoped F821/F822/F823, compile and diff checks PASS. Repeat QA and hosted diagnostic evidence remain separate gates; final mandatory full PR CI has NOT RUN. Production unchanged.

Affected38985283962 atf11c7bf again failed only the packet case. New trace establishes checkpoint phase projections_cleaned but first15 events were omitted by the existing16KiB worker-log tail exporter; exact originating operation remains UNKNOWN. This is an evidence-export limitation, not justification to change recovery. Startup-scale passed45s hard gate at43.3636s with a small margin; API lifetime HWM1016540KiB, not cgroup aggregate. No production performance claim.

Capture now preserves the first48 closed-schema events in a private fixed JSONL under isolated state/run and exports only strictly validated fields through existing native diagnostics, independently of log noise/truncation. Restart retains sequence/cap; symlinks, ownership/mode violations, malformed/schema-invalid/oversized input are rejected. Full contract135 PASS; independent QA3 focused PASS plus unowned/mode/malformed directory/file rejection checks PASSED on frozen three-file correction. Scoped compile/Ruff/diff checks PASS. Native diagnostic rerun remains pending; no application behavior changed.

### Bound-checkpoint recovery before a subsequent discovery write

Affected run38987293840 at3a229e5 reproduced the sole packet failure with the complete bounded lifecycle trace. Four subscription-refresh checkpoints closed successfully. The fifth refresh checkpoint reached projections_cleaned, then restore refused mihomo_preimage_policy_unproven and selection verification failed. Later provider Emergency Direct→VPN re-entry and its traffic checks succeeded; the unresolved earlier checkpoint then refused stale selection fences and an inventory-unbound current-projection recovery. The precise originating refresh trigger and underlying preimage-proof predicate remain UNKNOWN; none of those refusals is bypassed.

The corrective package resolves an existing checkpoint through its established generation owner before fetching or persisting another refresh. The short writer guard is released before provider network work. Ordinary inventory writes and source deletion recheck under the writer guard: every unresolved checkpoint blocks mutation, including verified-bound or empty inventory, because a new write could otherwise invalidate that checkpoint's binding. Recovery is not performed inside an inventory SQL transaction. A checkpoint created during provider fetch therefore yields a typed blocked outcome without changing inventory or its generation marker. No schema or durability changes are introduced.

The packet acceptance now explicitly proves recovery of the original still-bound candidate before ordinary new discovery. The hosted D→E lifecycle scenario first proves E cannot change inventory/native/last-good/identities while D remains open, verifies D through its owner, and only then publishes E. Reviewed identity-migration supersession retains exact checkpoint/plan/ledger proofs; its historical pre-fix partial-apply fixture is explicitly reconstructed offline while the worker is stopped, rather than pretending ordinary current writes may bypass the guard. All data are isolated synthetic fixtures.

Local grouped tests:247 PASS /5 FAIL; the same five failures reproduce on archived3a229e5 in the matched environment (four preimage/derived-row restore assertions and one subprocess denied by the ordinary isolation profile). They are not reported as PASS or waived hosted gates. Independent QA reproduced247 PASS with those five deselected and separately established the baseline match. Hosted native qualification and final full required CI remain NOT RUN for this corrective package.

Independent QA requested one further related correction: normal reconciliation checked managed/enabled ownership after resolving an open checkpoint, allowing its restore path to reload an optional disabled/external adapter. The preflight-specific ownership guard alone was insufficient. Direct-call regression coverage and an earlier owner/lifecycle check are being completed before renewed frozen review. Production remains unchanged.

The completed ownership correction checks managed/enabled lifecycle before checkpoint resolution in both callers. Test-first direct-reconcile cases reproduced the prior defect, then proved disabled/managed and enabled/external modes leave checkpoint, inventory and native calls unchanged. Managed/enabled startup recovery remains covered. Scoped lint also caught a helper out of scope in the changed hosted migration fixture; it now reads the real endpoint DTO. Synthetic subject deletion occurs only after owner recovery, preserving the checkpoint source fingerprint during proof.

Independent QA PASSED frozen corrective diff ee9cc240a131614afe95f5ef3b7c957135dcf9d207e6128b7c25e449a19862a8. QA independently ran249 component tests PASS with the five previously matched baseline failures deselected, scoped Ruff F821/F822/F823 and diff-check PASS. Writer's expanded invocation was249 PASS/5 known baseline/profile failures, not an all-green local suite. Hosted native and final mandatory full PR CI are still outstanding. The45-second startup and2-GiB memory gates remain unchanged; prior43.3636-second startup has limited margin and is an open risk until final-source measurements.
