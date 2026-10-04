# FWRouter engineering roadmap mirror

Updated 2026-10-04. This is an English project-owned mirror, not a second authority. The sole active plan is the [canonical FWRouter roadmap](/решения/roadmap/fwrouter/ROADMAP.md). The former mirror is preserved byte-for-byte at [dated history](history/ROADMAP_PRE_2026-10-04_RECOVERY_RESEQUENCE.md); the former canonical roadmap is archived in the canonical history directory. Historical status wording is not current status.

## Current evidence and boundaries

### Xray generation recovery — scoped complete

Source: `001c6e9`, `95f54c1`, `5728d85`, `a139e66`. Tests: full raw 1,322 passed, 52 failures matching the approved baseline IDs, 1 skipped; relevant cohort 603 passed, 23 baseline failures, 1 skipped. Separate pinned Mihomo, actual Xray archive/readback, and public-wrapper/idempotence gates passed. Deploy: standard backend/docs workflow; API startup performed owned recovery. Live: the current 78-identity generation was verified through current DB/public/binding/native readback and checkpoint receipt/closure; a later ordinary refresh was a verified no-op. A changed-input live generation was not forced. The October 2 initial exception cause remains unproven. One historical subscription-refresh unit failure predates deployment; its next scheduled run was not observed. See the [dated acceptance report](audits/xray_generation_recovery_2026-10-04/REPORT.md).

### Provider-managed source and protocol foundation

Current source stores provider bindings, credentials, observations and applied state per `source_ref`; it is not a singleton `.env` provider setting. The eight-profile Protocol Adapter and mixed per-entry dispatch are source/native-supported. Current-account live evidence covers the observed Hy2 path; seven other provider handshakes remain unverified. Emergency Direct/provider API error classification and fenced re-entry are the next correction, not a completed gate. See the [current provider contract](PROJECT_MAP/PROVIDER_MANAGED_SUBSCRIPTIONS_AND_ADAPTERS.md); previous singleton/env/pending-intersection wording is preserved in [dated history](PROJECT_MAP/history/PROVIDER_SPEC_PRE_2026-10-04_RECOVERY_RESEQUENCE.md).

### Test Architecture & CI/CD

The documentation contract is [Test Architecture & CI/CD Foundation](PROJECT_MAP/TEST_ARCHITECTURE_AND_CICD_FOUNDATION.md), with a pointer from `backend/tests/README.md`. Actual markers, affected-test automation, CI workflows, artifact retention, protected deploy gates and staging implementation remain open. Do not run the full suite after every fix. Only classified baseline failures may be excepted, and only with durable reviewable evidence; `/tmp` alone is not durable.

## Canonical execution order

The following order matches the canonical roadmap exactly. Do not permute stages.

1. Xray generation recovery correction — scoped complete; changed-input live acceptance open; October 2 initial cause unproven.
2. Emergency Direct/provider API evidence correction — planned/open across Source, Tests, Commit, Deploy and Live.
3. Test Architecture & CI/CD contract — documentation contract; implementation/open gates remain distinct.
4. Roadmap/spec/maps/English documentation reconciliation.
5. Stage 4 Performance & Resource Efficiency Audit.
6. Measured performance fixes only, with before/after evidence and a minimal CPU/RAM/SSD footprint invariant.
7. Stage 5 configuration/persistence contract: environment, SQLite intent, generated state, installer, clean install, backup/restore/rollback/upgrades.
8. Database Architecture & Integrity Audit.
9. Evidence-based database fixes, with DB-only triggers excluded from runtime/network/selector/recovery/Health/provider/Xray/Mihomo decisions.
10. Stage 6 dead/obsolete compatibility plus security and data-handling cleanup.
11. Stage 6A dead execution paths and measured internal work, using Stage 4 evidence rather than a second performance backlog.
12. Stage 8 Core/Modules ownership/API/events/config/lifecycle/Health contracts.
13. Stage 7 physical module extraction after those contracts.
14. Post-extraction functional/architecture audit.
15. Hardening and final runtime/config/deployment stabilization.
16. Final UI redesign with isolated implementation, parity and approved cutover.
17. Production Documentation, including complete technical Test Architecture/CI/CD documentation after implementation.
18. Stage 9 failure/recovery validation in disposable staging.
19. Stage 10 final release audit and initial supported production baseline.

## Performance and observability boundary

Measure API/database latency and query counts, CPU/RAM, SQLite/WAL I/O, SSD writes and storage growth, payload/request duplication, polling/timers/jobs/logging, process/container overhead, wakeups and adapter/runtime work. Fix only measured bottlenecks. Stage 6A consumes Stage 4 evidence. Keep a minimal CPU/RAM/SSD footprint; do not pre-optimize for a hypothetical future version. A separate server-metrics project may collect host/container resources. FWRouter may export domain metrics and consume an external observability source, but should not duplicate host/container collection.

## Preserved open work

- Xray changed-input live acceptance and the next natural scheduled refresh remain open; a prior systemd refresh unit failure is historical and was not reset or retried to obtain a green status.
- External Connections CRUD/lifecycle still needs isolated verification; remaining Rules/External Connections typed audit coverage needs scoped confirmation.
- Confirm Settings inventory race only if reproduced; measure LAN activity TTL without extending it to Xray/external/service clients.
- Confirm direct manual removal of the current effective vpn-auto member and subsequent fixed-target transition.
- Decide Tailscale peer admission/inventory/per-subject policy from routed evidence or managed intent.
- Preserve historical Xray 86→85 identity and WAN HTTPS-reset causes as unconfirmed; do not restore counts or assign causation without identity-level evidence.
- Review SQLite projection-commit/postimage-fsync crash window in Stage 9; pending markers must prevent false-ready publication.
- Review historical profile token/client-token handling and backup retention/access/restore policy; keep this security/data-handling scope explicit.
- Existing SSH 15-second timer readiness redesign and Xray gateway/API lifecycle coupling remain open. They are not solved by selection concurrency evidence.
- Post-release Xray subscription/client metadata compatibility/security research remains research, not a commitment to invent usage/quota/expiry data.

## Deferred future branch

Do not prebuild User mode/Admin-only UI removal, traffic analytics/top domains/history graphs, expanded Xray accounting, new traffic schema, or architecture for those features. Finish and release the current version first. The Minisk Rescue API remains in its own roadmap and is not combined with FWRouter.
