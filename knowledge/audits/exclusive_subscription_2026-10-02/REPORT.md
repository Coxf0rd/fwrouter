# Exclusive subscription VPN-auto acceptance — 2026-10-02

## Contract and source

Separate singleton subscription intent in existing settings (`vpn_auto_exclusive_source_ref`, stable source reference), atomic replace/disable, schema 23 unchanged. An ordinary source supplies all its eligible logical targets; an enabled provider-managed source supplies only its logical root. Canonical ownership/eligibility filters selector, generated Auto group, bootstrap/watchdog and provider recovery/re-entry. Fixed Xray export/profile eligibility explicitly remains unscoped; internal provider members and managed legacy entries remain excluded.

Admin retains all ordinary rows, names and local Health with Auto-only grey exclusion, disabled Auto/priority and available visibility/fixed selection. Managed legacy retains its distinct disabled state. Settings uses the selected saved source, existing ActionManager/jobs, localized RU/EN controls/status/errors and no provider polling.

Explicit action reuses subscription lock, shared writer guard, common Mihomo validation/promotion/recovery and exact pool/selection readback. It does not fetch provider configuration or mutate/switch provider members. Failed verification keeps intent pending and uses existing generation/selection checkpoints to preserve last-good. Stale disable cannot clear another source. Exclusive source deletion is rejected until explicit clear/replace. Provider recovery remains bounded; exhausted confirmed recovery uses existing Emergency Direct without clearing intent or falling back to another source.

## Tests and review

- 231 core selector/watchdog/runtime/provider/Mihomo regression tests passed.
- 110 ordinary/source/Xray regression tests passed; one known baseline `test_vpn_auto_reconcile_removes_stale_identity_and_recreates_one_stable_identity` still fails on absent `deleted_count`. It is separate from this change.
- 63 Protocol Adapter/universal/native/provider-protocol tests passed.
- Nineteen exclusive/API tests and 32 provider configuration/Admin projection tests passed. API lock/deduplication tests rechecked after correcting the test stub constructor.
- Nine targeted Node suites passed: exclusive, Admin list, User list, provider controls/members, Settings/Admin action integration, common error and data loading.
- Final diff review and clean surface checks completed before source commit. Startup reason regression found during development was corrected and rechecked; ordinary missing-target semantics retained.

Failure/exhaustion/replace/disable/multiple ownership tests are isolated fixtures/mocks; they are not production outage evidence. Protocol handshakes not verified by the preceding stage remain separate gates.

## Commit / deployment / live

Source commit `138010c` deployed through the standard backend/UI/docs installer. Config/module preflight passed before restart; `.env` hash unchanged, mode 0600. Nineteen changed backend/UI hashes match live. Only API explicitly restarted for deployment, then again for intent/startup persistence. The existing reconcile restarted Mihomo as required to apply the changed Auto group; Xray was not explicitly restarted. Schema remains 23, startup complete with no traceback/migration errors. Pre-deploy baseline: Health healthy/schema 23; all four required units active; generated/active Mihomo and Xray hashes match and native validators pass; provider member 1456 / binding-applied revision 4, normal provider request counters empty. Global Auto currently Estonia, intentionally preserved until authorized exclusive enable.


Live exclusive enable through Settings: job `8e98dba3-9a4e-410b-b743-b5283987f55e` success, `runtime_verified=true`, exact candidate pool/selector readback confirmed. Logical Auto moved from Estonia to Provider vpn. Provider current/applied member 1456 and binding/applied revision 4 unchanged, zero provider requests/mutations. All 380 preference rows match the pre-enable snapshot. Actual runtime `vpn-auto` contains only the provider logical group plus existing non-candidate DIRECT sentinel; selected group is Provider VPN. Twenty-seven other independent targets are Auto-excluded and remain fixed-selectable; one historical provider legacy row remains fully managed/disabled.

Read-only fixed-target validation passes for 28 targets, including provider root and 27 excluded ordinary targets. Native Xray subscription exports retain five transport-compatible targets including virtual VPN-auto and ordinary targets; provider native Hysteria2 export remains outside the prior capability contract. Global control and Mihomo handoff semantics are preserved, not a claim of testing every new fixed-target handshake.

Live browser RU/EN at 1440/390 confirms excluded grey rows, disabled Auto/priority, available visibility/fixed actions, retained managed legacy, checked exclusive control for selected provider source and unchecked available toggle for ordinary sources. Admin names the active source using its safe registry display label; logical row remains Provider vpn. No document overflow, page/console errors or browser writes in read-only matrix. The only enable browser write was the explicit exclusive POST; backend terminal job readback independently confirmed success.

Initial post-enable checks: all four units active, Health healthy/schema 23, VPN/Xray/watchdog in_sync; generated-active Mihomo/Xray hashes match, both native validators pass. Normal local Provider Ping succeeded with zero provider API requests. The intent and Provider vpn selection survived the persistence restart. Four-minute observation (13 samples) found one stable active provider root, one candidate, structural validity true and zero provider requests in every sample. No ordinary fallback or oscillation occurred.


Final review identified an outcome-projection edge case: common Mihomo validation rejection does not populate `last_good_retained`; the exclusive job incorrectly defaulted this to false despite no promotion. Correction `8339007` derives retention from the absence of promotion, with a targeted regression using the common result shape. Nineteen exclusive/API cases pass. Standard backend redeploy and config preflight completed; only API explicitly restarted. No production validation failure/outage was induced. Final post-correction live readback is recorded below. The generic reconcile failure is also localized in the existing RU/EN API-error registry, with a targeted UI assertion and cache version update.


Final post-correction checks: all four services active, Health healthy/schema 23/drift zero, VPN/Xray/watchdog in_sync; generated-active Mihomo/Xray hashes match and both native validators pass. Source-live parity for all 19 affected backend/UI files confirmed before the final localization/docs checkpoint; environment preserved. Three additional snapshots (2026-10-01T20:32:09.497489+00:00 through 2026-10-01T20:32:54.663206+00:00) retain the same exclusive ref/current provider, one eligible candidate, structural validity true, watchdog in_sync and zero provider requests. Main four-minute window: 2026-10-01T20:24:33.324877+00:00 through 2026-10-01T20:28:37.630485+00:00.

Read-only browser matrix repeated after correction: RU/EN 1440/390, no overflow/page/console errors or writes. User Auto picker contains only Provider vpn; fixed picker retains 28 targets, including Provider vpn and 27 other targets, plus the existing Global control. Deployed backend independently validates each fixed target; Xray native export remains five compatible targets with virtual VPN-auto. Idempotent explicit enable/reconcile through the real UI completed with localized “Готово”, checked intent retained, zero provider requests, no other writes. The additional explicit action did not change provider identity/protocol/member.

Acceptance: Source/Tests/Commit/Deploy/Live confirmed for exclusive subscription scope. Targeted backend cohorts total 455 PASS (231 + 110 + 63 + 19 + 32); known unrelated Xray deleted_count baseline is one separate FAIL. Nine targeted UI suites pass, including rechecked error localization. Final review/surface check pass. Recovery exhaustion/partial/failed outcomes are fixture/mock evidence only. No provider PATCH, protocol change, provider member switch, forced outage or destructive production operation occurred. Seven prior protocol endpoint handshakes and unrelated baseline issues remain outside this correction.
