# FWRouter engineering roadmap mirror

Updated 2026-10-04 after source commit `178b471`. This is an English project-owned mirror, not a second authority. The sole active plan is the [canonical FWRouter roadmap](/решения/roadmap/fwrouter/ROADMAP.md). The exact former mirror is preserved at [dated history](history/ROADMAP_PRE_2026-10-04_TEST_CICD_RECONCILIATION.md); the exact former canonical roadmap is in its linked history directory. Historical status wording is not current status.

## Current evidence and boundaries

### Xray generation recovery — scoped complete

Source: `001c6e9`, `95f54c1`, `5728d85`, `a139e66`. Tests: historical raw run 1,322 passed, 52 failures with the same IDs as the `58e053a` comparison, 1 skipped; relevant cohort 603 passed, 23 failures, 1 skipped. These failures are not approved CI exceptions. See the durable current [baseline status](audits/test_architecture_cicd_2026-10-04/BASELINE_STATUS.json) for exact IDs and triage. Separate pinned Mihomo, actual Xray archive/readback, and public-wrapper/idempotence gates passed. Deploy: standard backend/docs workflow; API startup performed owned recovery. Live: current 78-identity generation was verified through current DB/public/binding/native readback and checkpoint receipt/closure; a later ordinary refresh was a verified no-op. A changed-input live generation was not forced. The October 2 initial exception cause remains unproven. One historical subscription-refresh unit failure predates deployment; its next scheduled run was not observed. See the [dated acceptance report](audits/xray_generation_recovery_2026-10-04/REPORT.md).

### Provider-managed source and protocol foundation

Current source stores provider bindings, credentials, observations and applied state per `source_ref`; it is not a singleton `.env` provider setting. Eight protocol profiles and mixed per-entry dispatch are implemented, tested and deployed. Current-account live evidence covers the observed Hysteria2 path; seven other provider handshakes remain unverified. Emergency Direct/provider API evidence correction is implemented and deployed; real fault/re-entry acceptance remains a Stage 9 gate. See the [current provider contract](PROJECT_MAP/PROVIDER_MANAGED_SUBSCRIPTIONS_AND_ADAPTERS.md); previous singleton/env wording is preserved in [dated history](PROJECT_MAP/history/PROVIDER_SPEC_PRE_2026-10-04_RECOVERY_RESEQUENCE.md).

### Test Architecture & CI/CD

Source/Tests/Commit are complete at `178b471`: 149 test files classified; L0–L7 taxonomy, versioned domain manifest, deterministic affected selection, exact-node classifier, fixture contracts, isolated smoke and GitHub Actions are present. Bounded local infrastructure acceptance passed; L6 was not run. Deploy is not applicable; no production deploy/restart occurred; Live was not run. Remote CI execution, locked-environment acceptance, native runtime provisioning, deploy-authorization/protected-deployment integration and disposable L7 acceptance remain open. Eleven baseline IDs retain a last-failed observation; no exceptions are approved, and there was no complete post-fix 52-ID run. See the [implementation report](audits/test_architecture_cicd_2026-10-04/REPORT.md), [baseline status](audits/test_architecture_cicd_2026-10-04/BASELINE_STATUS.json) and [technical contract](PROJECT_MAP/TEST_ARCHITECTURE_AND_CICD_FOUNDATION.md). Roadmap reconciliation is complete; Stage 4 is next. Do not run L6 after every fix. Use affected L0–L5 per policy; L7 is staging/release only. `/tmp` artifacts are provenance, not durable evidence.

## Canonical execution order

The following order matches the canonical roadmap exactly. Do not permute stages.

1. Xray generation recovery correction — scoped complete; changed-input live acceptance open; October 2 initial cause unproven.
2. Emergency Direct/provider API evidence correction — Source, Tests, Commit, backend/docs Deploy and bounded safe Live scope complete; real fault/PATCH/re-entry acceptance remains open for Stage 9.
3. Test Architecture & CI/CD Foundation — Source/Tests/Commit complete at `178b471`; no production deploy/live claim; remote CI, locked environment, native runtime provisioning, deploy authorization and disposable L7 remain open.
4. Roadmap/spec/maps/English documentation reconciliation — complete, documentation only.
4A. Provider automatic-switch policy/configured Auto membership — completed at `7478b02` (Source/affected Tests/Commit/standard Deploy/safe Live). Schema 24; [source/tests](audits/provider_auto_switch_policy_2026-10-05/REPORT.md), [live](audits/provider_auto_switch_policy_2026-10-05/LIVE.md), [UI](audits/provider_auto_switch_policy_2026-10-05/UI.md). Per-source policy defaults off (operator confirmed); manual switch remains allowed. Ordinary configured membership is editable under exclusive Provider without changing effective eligibility or causing runtime/provider work when the effective set is unchanged.
5. Stage 4 Performance & Resource Efficiency Audit — completed in bounded measured scope on 2026-10-05.
6. Measured performance fixes — next active milestone; evidence only, with before/after evidence and a minimal CPU/RAM/SSD footprint invariant.
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

Stage 4 is a read-only baseline of the current release: CPU/RAM; API latency; SQLite query latency/count and WAL/DB I/O; SSD writes/storage growth; polling/timers/jobs; logging volume; duplicate requests/work; process/container overhead; background wakeups; adapter/runtime cost. Fix only measured bottlenecks and require comparable before/after measurements for each fix. Stage 6A consumes the same evidence. Preserve a minimal CPU/RAM/SSD footprint. A separate server-metrics project may collect host/container resources; FWRouter should not duplicate that collector.

## Test policy for subsequent milestones

Implementation reports list the levels actually run and why. Default to affected L0 plus affected L1/L2 and required L3/L4. Run L5 when shared/domain contracts change. Do not run L6/full regression by default; use the explicit scheduled/manual, milestone, release, major-architecture or policy gate. L7 runs only in disposable staging/release acceptance and is excluded from normal CI.

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

### Dated clarification — Test Architecture & CI/CD, 2026-10-04

The earlier Step 3 wording below described implementation as wholly open. It is preserved here as history and superseded by the source/test checkpoint above:

> The documentation contract is [Test Architecture & CI/CD Foundation](PROJECT_MAP/TEST_ARCHITECTURE_AND_CICD_FOUNDATION.md), with a pointer from `backend/tests/README.md`. Actual markers, affected-test automation, CI workflows, artifact retention, protected deploy gates and staging implementation remain open. Do not run the full suite after every fix. Only classified baseline failures may be excepted, and only with durable reviewable evidence; `/tmp` alone is not durable.
>
> Execution order entry: “3. Test Architecture & CI/CD contract — documentation contract; implementation/open gates remain distinct.”

The source foundation and workflow definitions now exist, but this does not claim activated/green remote CI, production deployment, a full post-fix baseline rerun, deploy authorization integration, or L7 staging acceptance. The former text in this dated clarification is preserved exactly in the linked [pre-reconciliation mirror](history/ROADMAP_PRE_2026-10-04_TEST_CICD_RECONCILIATION.md).

### Deferred research

- Post-release Xray subscription/client metadata compatibility/security research remains research, not a commitment to invent usage/quota/expiry data.

### Documentation reconciliation — 2026-10-04

The Test Architecture & CI/CD Foundation Source/Tests/Commit checkpoint is complete at `178b471`; roadmap reconciliation is complete. The next active milestone is Stage 4 Performance & Resource Efficiency Audit. See the canonical roadmap for current status and evidence boundaries.

## Deferred future branch

Do not prebuild User mode/Admin-only UI removal, traffic analytics/top domains/history graphs, expanded Xray accounting, new traffic schema, or architecture for those features. Finish and release the current version first. The Minisk Rescue API remains in its own roadmap and is not combined with FWRouter.

### 2026-10-05 — functional correction before Performance

The requested automatic-switch policy/configured-membership correction precedes Stage 4. The exact previous mirror is preserved in [history](history/ROADMAP_PRE_2026-10-05_AUTO_SWITCH_POLICY.md); closed milestones are not reopened.

### 2026-10-05 — safe acceptance

`7478b02` was deployed using the backend/UI/docs installer; only API was explicitly restarted. Schema 24/default OFF, same provider member 1456 and material revisions 4/4, Auto revision 31/exclusive/provenance/effective pool preserved. Ordinary configured Auto UI write persisted; Core API restore recovered original preferences with no reconcile/apply/reselect. Provider counters zero; RU/EN 1440/390/no overflow/JS errors and native Mihomo/Xray validation/parity passed; 78/78 Xray identities/bindings preserved. Real outage/re-entry/remote mutation gates remain open under Stage 9. Stage 4 is next.

### 2026-10-05 — Stage 4 bounded audit

The read-only audit on `0b24cd1` / deployed application `7478b02` is complete in its measured scope: [report and artifacts](audits/performance_resource_2026-10-05/REPORT.md), [local consolidated report](</решения/аудит/аудит №1 2026-10-05.md>). Source: audit artifacts only. Tests: bounded measurements, isolated microbenchmarks and L0 artifact checks, not L1–L7 acceptance suites. Deploy: none. Live: bounded reads, no forced outage, refresh/apply or provider mutation.

No P0 confirmed. Confirmed candidates: broad Health before inventory role filter (cold narrow GET2.48s, warm11–26ms), schema initialization on health reads (isolated median4.15ms,77DDL/2DML), and no-expiry retention temp writes (synthetic2.02MB,median37.68ms). Six distinct role calls and current sub-ms SQL scans are not duplicate/index fixes. Missing native generation timing, complete HTTP query/serialization attribution, contention, background attribution and long-window SSD/WAL/log growth remain measurement gates.

Next active work is measured fixes: schema read/init boundary; cold inventory attribution/scoping; residual UI burst analysis; background/member/watchdog attribution; retention no-op writes; then only proven DB/write candidates. Require before/after evidence and affected L0–L5 policy, no default L6. Preserve remaining stage order and all runtime/ownership safety gates. The exact former roadmap is in [history](history/ROADMAP_PRE_2026-10-05_PERFORMANCE_AUDIT.md); older dated “Stage4 next” statements are historical and superseded by this entry.
