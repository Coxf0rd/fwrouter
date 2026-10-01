# Exclusive subscription VPN-auto acceptance — 2026-10-02

## Contract and source

Separate singleton subscription intent in existing settings (`vpn_auto_exclusive_source_ref`, stable source reference), atomic replace/disable, schema 23 unchanged. An ordinary source supplies all its eligible logical targets; an enabled provider-managed source supplies only its logical root. Canonical ownership/eligibility filters selector, generated Auto group, bootstrap/watchdog and provider recovery/re-entry. Fixed Xray export/profile eligibility explicitly remains unscoped; internal provider members and managed legacy entries remain excluded.

Admin retains all ordinary rows, names and local Health with Auto-only grey exclusion, disabled Auto/priority and available visibility/fixed selection. Managed legacy retains its distinct disabled state. Settings uses the selected saved source, existing ActionManager/jobs, localized RU/EN controls/status/errors and no provider polling.

Explicit action reuses subscription lock, shared writer guard, common Mihomo validation/promotion/recovery and exact pool/selection readback. It does not fetch provider configuration or mutate/switch provider members. Failed verification keeps intent pending and uses existing generation/selection checkpoints to preserve last-good. Stale disable cannot clear another source. Exclusive source deletion is rejected until explicit clear/replace. Provider recovery remains bounded; exhausted confirmed recovery uses existing Emergency Direct without clearing intent or falling back to another source.

## Tests and review

- 231 core selector/watchdog/runtime/provider/Mihomo regression tests passed.
- 110 ordinary/source/Xray regression tests passed; one known baseline `test_vpn_auto_reconcile_removes_stale_identity_and_recreates_one_stable_identity` still fails on absent `deleted_count`. It is separate from this change.
- 63 Protocol Adapter/universal/native/provider-protocol tests passed.
- 49 exclusive/API/provider configuration/Admin projection tests passed, including 17 new exclusive/API cases. API lock/deduplication tests rechecked after correcting the test stub constructor.
- Nine targeted Node suites passed: exclusive, Admin list, User list, provider controls/members, Settings/Admin action integration, common error and data loading.
- Final diff review and clean surface checks completed before source commit. Startup reason regression found during development was corrected and rechecked; ordinary missing-target semantics retained.

Failure/exhaustion/replace/disable/multiple ownership tests are isolated fixtures/mocks; they are not production outage evidence. Protocol handshakes not verified by the preceding stage remain separate gates.

## Commit / deployment / live

Pending the source checkpoint and controlled deployment. Pre-deploy baseline: Health healthy/schema 23; all four required units active; generated/active Mihomo and Xray hashes match and native validators pass; provider member 1456 / binding-applied revision 4, normal provider request counters empty. Global Auto currently Estonia, intentionally preserved until authorized exclusive enable.
