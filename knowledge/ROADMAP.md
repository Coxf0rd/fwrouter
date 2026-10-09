# FWRouter engineering roadmap mirror

Updated 2026-10-09: Test Architecture & CI Stabilization is active. Phase A delivered; Phase B accepted in bounded source/local-test scope; Phase C source complete. Phase D implementation was authorized and is present on `stage/test-architecture-ci-stabilization`. Run [37934872853](https://github.com/Coxf0rd/fwrouter/actions/runs/37934872853), head `b580b2e`, completed **BLOCKED**: affected L0–L3 1,112 PASS / 129 FAIL with mandatory L4/L5 absent/incomplete; the 37-case functional cohort finished 23 PASS / 14 FAIL. Qualified Mihomo passed 26/26, isolated Docker Xray 1/1, and minimum application Xray readback 1/1; these scoped passes do not close the affected/functional gates. The prior seven-ID cohort passed 7/7 in run [37904598829](https://github.com/Coxf0rd/fwrouter/actions/runs/37904598829); this run reports five PASS and two skipped/uncollected from those IDs. Phase E remains OPEN. No production deploy/restart occurred; CD remains deferred. See [full checkpoint](audits/test_architecture_phase_d_2026-10-09/REPORT.md), [structured receipt](audits/test_architecture_phase_d_2026-10-09/LAST_FULL_CHECKPOINT.json), and [failure analysis](audits/test_architecture_phase_d_2026-10-09/FUNCTIONAL_FAILURES_37934872853.csv). Host TPROXY and nonlocal handoff remain NOT RUN; run 37934437529 logged `Listener fwrouter-tproxy listen err: operation not permitted`, `Listener fwrouter-full-tproxy listen err: operation not permitted`, and `Listener fwrouter-xray-egress-32579334990b listen err: listen tcp 172.18.0.1:53216: bind: cannot assign requested address`. Functional PASS would not prove these capabilities.

## 2026-10-09 — Phase C: SOURCE COMPLETE / EXECUTION PENDING

Phase C mandatory source gaps from checkpoint `c25ef4c` are implemented and registered: 31 application definitions / 48 cases (37 functional L3, 11 explicit L7), actual Xray generation/checkpoint/native proof, Core/provider/Mihomo recovery/fences/CAS, SQLite migration/rollback and real Chromium UI→API→state/error tests. Local 60 pure harness/gate contracts PASS; hosted native/browser/crash cases are **NOT RUN — pending Phase D**. This does not close full application acceptance. Seven historical failures remain OPEN. Phase D/E are not started and require separate user authorization. Operational `6004400` remains PAUSED/BLOCKED; no workflow, deploy, runtime or application behavior changes.

See [source completion report](/srv/fwrouter/knowledge/audits/test_architecture_phase_c_2026-10-08/REPORT.md), [exact traceability](/srv/fwrouter/knowledge/audits/test_architecture_phase_c_2026-10-08/TRACEABILITY_MATRIX.csv), [checkpoint history](/srv/fwrouter/knowledge/audits/test_architecture_phase_c_2026-10-08/CHECKPOINT_C25EF4C_REPORT.md) and [prior roadmap](/srv/fwrouter/knowledge/history/CANONICAL_ROADMAP_PRE_PHASE_C_EXTENSION_2026-10-09.md).


## Current evidence and boundaries

### Xray generation recovery — scoped complete

Source: `001c6e9`, `95f54c1`, `5728d85`, `a139e66`. Tests: historical raw run 1,322 passed, 52 failures with the same IDs as the `58e053a` comparison, 1 skipped; relevant cohort 603 passed, 23 failures, 1 skipped. These failures are not approved CI exceptions. See the durable current [baseline status](audits/test_architecture_cicd_2026-10-04/BASELINE_STATUS.json) for exact IDs and triage. Separate pinned Mihomo, actual Xray archive/readback, and public-wrapper/idempotence gates passed. Deploy: standard backend/docs workflow; API startup performed owned recovery. Live: current 78-identity generation was verified through current DB/public/binding/native readback and checkpoint receipt/closure; a later ordinary refresh was a verified no-op. A changed-input live generation was not forced. The October 2 initial exception cause remains unproven. One historical subscription-refresh unit failure predates deployment; its next scheduled run was not observed. See the [dated acceptance report](audits/xray_generation_recovery_2026-10-04/REPORT.md).

### Provider-managed source and protocol foundation

Current source stores provider bindings, credentials, observations and applied state per `source_ref`; it is not a singleton `.env` provider setting. Eight protocol profiles and mixed per-entry dispatch are implemented, tested and deployed. Current-account live evidence covers the observed Hysteria2 path; seven other provider handshakes remain unverified. Emergency Direct/provider API evidence correction is implemented and deployed; real fault/re-entry acceptance remains a Stage 9 gate. See the [current provider contract](PROJECT_MAP/PROVIDER_MANAGED_SUBSCRIPTIONS_AND_ADAPTERS.md); previous singleton/env wording is preserved in [dated history](PROJECT_MAP/history/PROVIDER_SPEC_PRE_2026-10-04_RECOVERY_RESEQUENCE.md).

### Test Architecture & CI/CD

Source/Tests/Commit are complete at `178b471`: 149 test files classified; L0–L7 taxonomy, versioned domain manifest, deterministic affected selection, exact-node classifier, fixture contracts, isolated smoke and GitHub Actions are present. Bounded local infrastructure acceptance passed; L6 was not run. Deploy is not applicable; no production deploy/restart occurred; Live was not run. Remote CI execution, locked-environment acceptance, native runtime provisioning, deploy-authorization/protected-deployment integration and disposable L7 acceptance remain open. Eleven baseline IDs retain a last-failed observation; no exceptions are approved, and there was no complete post-fix 52-ID run. See the [implementation report](audits/test_architecture_cicd_2026-10-04/REPORT.md), [baseline status](audits/test_architecture_cicd_2026-10-04/BASELINE_STATUS.json) and [technical contract](PROJECT_MAP/TEST_ARCHITECTURE_AND_CICD_FOUNDATION.md). Roadmap reconciliation is complete; the later dated Stage 4/4B completion supersedes this earlier next-stage statement. Do not run L6 after every fix. Use affected L0–L5 per policy; L7 is staging/release only. `/tmp` artifacts are provenance, not durable evidence.

## Active execution order

Only this sequence is active. Dated entries below record evidence at the time and do not define today’s plan. Prior wording is preserved in [history](history/README.md), including the [exact canonical snapshot before this resequencing](history/CANONICAL_ROADMAP_PRE_2026-10-08_CI_STABILIZATION.md) and [the former English mirror](history/ROADMAP_PRE_2026-10-08_CI_STABILIZATION.md).

1. **Test Architecture & CI Stabilization — active milestone.** Phase A delivered; Phase B accepted in bounded source/local-test scope; Phase C source complete. Phase D implementation is separately authorized and implemented on `stage/test-architecture-ci-stabilization`. Run 37934872853 completed BLOCKED: affected L0–L3 1,112 PASS / 129 FAIL with L4/L5 absent/incomplete; functional 37-case cohort 23 PASS / 14 FAIL. Qualified Mihomo 26/26, isolated Xray 1/1, and minimum application Xray readback 1/1 pass but do not satisfy the failed mandatory gates. Phase E remains OPEN. No production deploy/restart or CD.
2. **Operational Performance Fixes — PAUSED/BLOCKED.** Preserve local branch `stage/operational-performance-fixes` at `6004400`; do not transfer its commits into the CI milestone branch. Packages 1–2 contain implemented changes and bounded deployment evidence; Package 3 is unimplemented and conditional. No PR/merge until full application staging acceptance. After the CI milestone is accepted through the repository review process, return to the operational branch, integrate the resulting current `main`, run mandatory full staging CRUD/crash/restart/recovery acceptance, then verify correctness and comparable performance. Close the milestone and authorize a separate PR/manual merge only after PASS. On failure, retain BLOCKED and enumerate the exact gates.
3. **Stage 5 — configuration and persistence contracts.** Start after Operational Performance Fixes is accepted. Define env/SQLite intent/generated artifacts, installer, clean install, backup/restore/rollback and supported upgrade/release contracts.
4. **Database Architecture & Integrity Audit.** Tables/relations, canonical/derived/cache data, constraints, indexes/scans, stale/orphan rows, transactions/WAL/CAS, writes/growth/contention, migrations and trigger candidates.
5. **Evidence-based Database fixes.** Only issues proven by the audit; Core/runtime/network/selector/recovery/Health/provider/Mihomo/Xray ownership stays out of triggers.
6. **Stage 6 — dead/obsolete compatibility and security/data-handling cleanup.**
7. **Stage 6A — measured internal execution cleanup.** Reuse Stage 4 evidence; do not create an independent performance backlog.
8. **Stage 8 — Core ↔ Modules contracts** for ownership, API/events/config/lifecycle/Health.
9. **Stage 7 — physical module extraction** only after Stage 8.
10. **Post-extraction functional/architecture audit.**
11. **Hardening and final runtime/config/deployment stabilization.**
12. **External Telemetry Ingestion / Metrics and Traffic Accounting — a late, separate milestone** after extraction and DB/architecture stabilization. FWRouter exports domain evidence only; host/container metrics belong to a separate observability project. Do not prepare future-version schema or analytics in advance.
13. **Final UI redesign.**
14. **Production Documentation**, including full technical documentation for the accepted test/CI system.
15. **Stage 9 — disposable staging failure/recovery validation.**
16. **Stage 10 — final release audit.**

**CD is outside this execution order and deferred until a separate decision.** Until then, production deployment remains manual through the existing installer: `/srv/fwrouter/installer/install.sh --deploy --component backend --component ui` (add `--component docs` for documentation changes). Deploy mode copies selected components and does not restart services. When backend changes require applying, restart the API separately with `systemctl restart fwrouter-api.service`; docs-only deployment needs no restart. Future CD is a separate project requiring a protected environment/approval, exact-commit promotion, readiness/Health/native parity/smoke and proven rollback; GitHub Actions must not automatically deploy production now.

## Test policy for every subsequent implementation milestone

Reports list the test levels actually run and why. Default to affected L0, affected L1/L2 and required L3/L4; use L5 when shared/domain contracts change. L6 is not the default; run it only at a scheduled/manual/milestone/release/policy gate. L7 is separate staging/release acceptance after compatibility of the isolated environment has been confirmed. A plan, dry-run or partial selection is not a passing gate.

## Performance and observability boundary

Stage 4 is a read-only baseline of the current release: CPU/RAM; API latency; SQLite query latency/count and WAL/DB I/O; SSD writes/storage growth; polling/timers/jobs; logging volume; duplicate requests/work; process/container overhead; background wakeups; adapter/runtime cost. Fix only measured bottlenecks and require comparable before/after measurements for each fix. Stage 6A consumes the same evidence. Preserve a minimal CPU/RAM/SSD footprint. A separate server-metrics project may collect host/container resources; FWRouter should not duplicate that collector.

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

## Dated evidence history — reference only, not the active execution order

The dated entries below preserve historical observations and status. Any “next milestone” wording is superseded by the active order above.

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

### 2026-10-05 — UI audit supplement and Stage 4B

UI/source attribution on `6ef940b` supplements the [Stage4 report](audits/performance_resource_2026-10-05/REPORT.md): [browser evidence](audits/performance_resource_2026-10-05/UI_PERFORMANCE.md) and [execution paths](audits/performance_resource_2026-10-05/UI_EXECUTION_PATHS.md). No application fixes/deploy/restart or provider mutations. Previous wording is preserved exactly in [history](history/ROADMAP_PRE_2026-10-05_UI_PERFORMANCE_4B.md).

**Stage4B Performance & Resource Fixes** is now the explicit next active milestone; fixes Source/Tests/Commit/Deploy/Live remain open. P0 none confirmed. P1 cold inventory/readiness and prior server burst require component/cache/queue attribution before scoped fixes. Different inventory roles are not duplicates, and Promise.all alone is not a defect. P2 proven small costs: Health schema initializer (isolated77DDL/2DML,median4.15ms) and no-expiry retention temp rewrite (synthetic2.02MB,median37.68ms). Background/native/HTTP-SQL/serialization/heap/write-cadence/contention/long-window storage attribution remain measurement prerequisites, not presumed fixes. No blanket TTL/compression/lazy/virtualization/fallback changes.

One order: schema read/init boundary; cold inventory attribution/scoping; residual UI/server burst/render; background/member/watchdog attribution; retention no-expiry writes; then only proven DB/write candidates. Each fix requires comparable before/after source/runtime/data/workload/cache evidence and relevant latency/CPU/query/request/payload/write counts. Separate first usable content from complete hydration and observer settling. TTFB does not identify DB/handler cost. Preserve canonical Health, Core/revision/incarnation/readback/CAS, exclusive intent/fixed Xray, provider semantics and last-good. Use affected L0–L5, no default L6; L7 disposable staging only. Stage5 remains immediately after accepted Stage4B scope closes; subsequent stage order is unchanged.

### UI evidence addendum — complementary Settings observation

A single supplementary Settings document flow on `6ef940b` recorded an ambiguous initial empty/error journal marker at 8.669s (not proven usable content or full workspace readiness), cached journal tab returns 305/368ms and locale change 314ms with zero new requests. Completed static ResourceTiming bytes 2.36MB (background PNG 1.28MB, loopback download 8.4ms) are a P2 footprint review candidate, not a proven multi-second latency cause or permission to redesign/remove assets. Full workspace completion, true warm HTTP page cache and remote/mobile performance remain open. [Evidence](/srv/fwrouter/knowledge/audits/performance_resource_2026-10-05/UI_SETTINGS_COMPLEMENT.json). Stage4B includes contract-safe payload/asset review only after attribution and requires before/after; priority order and Stage5 dependency remain unchanged.

### 2026-10-05 — Stage4B first measured fix batch

Baseline `24ef1ba`; [implementation/attribution evidence](audits/performance_fixes_2026-10-05/REPORT.md). Three measured backend fixes separate schema observation from initialization, bound technical-log candidates before recursive sanitization, and skip no-expiry temporary retention writes. Global Health/cache semantics and all routing/provider/Xray ownership contracts remain intact; UI and Auto sorting are unchanged. Cold inventory is dominated by native runtime enforcement; workspace is dominated by technical-log processing. Remaining burst/scheduling/background and long-window resource gates stay open.

Affected L0–L5 policy applies, including isolated L4 smoke; eight selected failures reproduce on clean `24ef1ba` and remain visible rather than being blanket-allowlisted. L6/destructive L7 are not run. Source/Tests/Commit/Deploy/Live stay distinct; Stage4B is not wholly closed and Stage5 is not started. The exact pre-batch mirror is preserved in [history](history/ROADMAP_PRE_2026-10-05_STAGE4B_BATCH1.md).


### 2026-10-05 — Stage 4B first batch deployed, bounded live acceptance

Source `9a4dc03`: schema read/init separation, bounded technical-log sanitization, no-expiry retention write avoidance. Source/affected Tests/Commit/standard backend-docs Deploy/bounded Live complete; related units healthy, schema24/revision37/Provider member1456/exclusive/provenance unchanged, native configs and 78 binding/10 listener semantics preserved. Receipt timestamps alone changed. [Delivery evidence](/srv/fwrouter/knowledge/audits/performance_fixes_2026-10-05/LIVE.md).

Comparable isolated helper results: logs median4377.81→173.04ms (n3, identical payload); schema3.052→1.827ms with0DDL/0DML (n5); no-expiry retention8.55→5.57ms and2.02MB→0 temporary writes/call (n5). Single UI flow workspace TTFB4835.3→601.9ms/Admin phase5270→1047ms; User/Settings improvement and `/servers` burst resolution are not demonstrated. No full-suite green or host-wide resource claim. Eight exact baseline failures remain visible; no L6/L7 run. Stage4B stays OPEN: residual Health/native, burst/scheduling/background and long-window resource attribution; Stage5 still follows its accepted closure. No Auto sorting fix included.


### 2026-10-05 — Stage 4B batch 2: applied-status native validation boundary

Baseline `1397f50`. Confirmed source-level unnecessary work: ordinary applied status passed `applied.nft` as a new candidate, triggering `nft -c` and a temporary copy on cache misses. The bounded correction removes only that repeat candidate validation. Live native table/chains, routing, counters, marker parity, mode observation and freshness remain; explicit apply/candidate-only checks retain strict native validation. No Core/provider/exclusive/Xray intent or scheduler/WAL policy change. [Batch 2 evidence](/srv/fwrouter/knowledge/audits/performance_fixes_batch2_2026-10-05/REPORT.md). Prior plan preserved in [history](history/ROADMAP_PRE_2026-10-05_STAGE4B_BATCH2.md).

Initial quantitative normal-read window (same live API process, serial servers/dry-selector/router-summary/Health plus 120s passive observation): provider requests/discoveries/mutations remain zero. Process-accounted writes and cancelled writes are distinct from DB-only or physical SSD writes; destination ownership and long-window WAL/device amplification remain open. No write suppression, cache/index or background scheduling change is justified by those counters alone. Historical `/servers` burst causality remains unproven. Source/Tests/Commit/Deploy/Live acceptance is recorded separately in the batch report. Stage4B remains active; Stage5 and Auto sorting are outside this batch.


### 2026-10-07 — Stage 4B package 03a64a1 acceptance reconciliation

Source/affected Tests/Commit: `03a64a1`. Standard backend/docs Deploy and API-only restart completed October 5; this acceptance does not redeploy or restart. [Dated delivery evidence](/srv/fwrouter/knowledge/audits/performance_fixes_batch2_2026-10-05/ACCEPTANCE_OCT7.md) records October 7 read-only verification and the interrupted immediate postrestart capture.

Confirmed: Health/router-summary 3/3 HTTP200 each, applied-status source/deployed parity and omission of duplicate candidate `nft -c`, unchanged applied nft/manifest, current generated/mounted Mihomo/Xray parity, same Provider member1456/revisions4/4/exclusive/active provenance, and zero provider request/discovery/mutation delta during the bracketed normal GET/UI window (11 historical targeted-refresh requests remain cumulative). Xray currently has71 bindings/9 listeners matching current canonical inventory and a successful refresh generation, rather than historical78/10. Single-run UI timings and natural-cache inventory measurements are not a controlled whole-page acceleration claim.

Overall acceptance remains **PARTIAL**, not a regression finding: revision37→65 lacks retained transition attribution, aggregate member events cannot exclude intervening oscillation, and scheduled DNS recovery is classified but not causally attributed to this package. Current-state parity cannot prove continuous postdeploy behavior. Current Mihomo/Xray native validation PASS (exit0); global routing intent matches the protected predeploy snapshot. Full historical per-client fixed-binding continuity is not established. Only necessary L0/read-only L4 acceptance and retained affected L1–L5 evidence; no L6/L7, new optimization or unrelated baseline rerun. Stage4B remains OPEN; residual Health, `/servers` burst, WAL/SSD, baseline failing IDs and Stage5 are untouched.


### 2026-10-07 — Stage 4B revision/background attribution (baseline 3fc496a)

[Bounded source/history and isolated measurement report](/srv/fwrouter/knowledge/audits/revision_background_attribution_2026-10-07/REPORT.md). Revision37→65 is consistent with14 staged Mihomo promote/restart pairs:22 increments44–65 directly occur in8 retained successful refresh receipts;6 increments38–43 are expected/inferred from3 earlier scheduled promotion workflows whose numeric job receipts are absent. The fence also tracks config/runtime mutations; it is not a server-switch counter. No unsupported claim of individual numeric receipt coverage for those6 increments.

Identity-aware member history finds repeated alternation within2 ordinary Mihomo fallback groups (outside the exclusive pool/current Provider target). These are runtime-native observations, not proof of global VPN-auto oscillation or FWRouter switch commands; the exact fallback trigger remains unknown. Provider/exclusive/active provenance endpoints are unchanged, and no retained global switch evidence was found. This does not establish continuous state across the retention gap.

Isolated fence-only replay:28 write transactions/upserts,25.978ms,131872B temporary SQLite WAL; not production SSD bytes or a full generation-cost benchmark. No confirmed costly redundant work justifies a routing/recovery/fence fix here. Same-value topology writes remain an unquantified source finding rather than an authorized optimization. Existing same-PID normal GET/UI provider bracket records delta0; it is point-window evidence, not a lifetime zero claim. No application changes/deploy/restart/provider mutation, unrelated baseline rerun or Stage5. Stage4B remains OPEN for previously recorded performance/resource gates and the explicit historical/member-trigger limits.


### 2026-10-07 — Stage 4B residual runtime audit and scoped read-model admission

Baseline `8d137b7`; [consolidated execution-path audit](/srv/fwrouter/knowledge/audits/residual_runtime_2026-10-07/REPORT.md). Pinned Mihomo1.19.31 source establishes fallback first-alive selection from cached health history and generated300s native checking; exact historical health failures/probe overlap remain unverified. Both old ordinary groups are now missing, absent from runtime and excluded by active-inventory background gates. Do not treat their member observations as global VPN-auto oscillation.

One minimal Source/affected Tests correction defers derived Xray binding-file reads until an explicit client passes path/target/active gates. Active fixed targets and stablevpn-global semantics are unchanged; no cache/state/schema/provider/selector/runtime change. Matched synthetic315-subject/n4 fixture:524→8 actual reads/parses, identical output, last-round14.216→2.102ms; not a live UI/API acceleration claim. Isolated L4 smoke and narrow scoped/Xray/health-probe regression checks pass; noL6/L7 or unrelated baseline rerun. The coherent source/evidence commit is this checkpoint; **Deploy and Live acceptance of the fix remain OPEN**, deployed application stays03a64a1.

New normal-path bracket: allGETs200, provider request/discovery/mutation/error/cache delta0 onPID1720736 (12 historical targeted configGETs). Live system-summary n1=1726.91ms vs private native-faked local projection n4=31.31–33.35ms; internal native/cache/queue cost is not attributed by this comparison. Source/mocked counts also identify unchanged staged candidate validations before apply-noop and unconditional maintenanceCLI bootstrap before schema-check/dry-run (DNSreconcile even whenstartup apply disabled). No fix of these broader boundaries, scheduling, logging/WAL or fallback is included.

Stage4B remains OPEN: admit maintenance commands safely in a separate ownership-reviewed checkpoint; then measure summary/native/guard queue cost, no-op generation cost, JSONL parsing and observation/jobWAL overhead before any additional fix. No production deploy/restart/outage/provider mutation; Stage5, Auto sorting and known unrelated failures remain untouched. Historical evidence and earlier acceptance limits remain preserved.


### 2026-10-07 — Stage 4B measured completion package, release acceptance pending

On baseline `49591c2`, the scoped-bindings admission fix has been deployed and boundedly accepted. The next coherent source package implements command-specific maintenance admission (no API bootstrap before schema-check/cleanup/rebuild), one-snapshot native nft counter reads with equivalent fallback projection, and same-operation exact-byte Mihomo validation reuse pinned to a local immutable image ID. No persistent cache, schema, selection ownership, routing/provider policy or cadence change. Required first native validation and final runtime/readback remain intact.

[Completion report](/srv/fwrouter/knowledge/audits/stage4b_completion_2026-10-07/REPORT.md): five counter commands median289.2ms→one median68.1ms (n3); complete new projection median80.1ms. Direct-route observer1534.5→1351.9ms (n1 each; SQL76 unchanged, payload7403B unchanged), nft calls9→5. Identical transition/final candidates require one native check rather than two (~720ms per check including Docker startup), plus image-ID lookup. Maintenance CLI no longer runs initializer/zero-age job cleanup/DNS/startup reconciliation before admission. Daily retention and its diagnostic canary are intentional; no new WAL/retention no-op defect was proved.

Source/affected L0–L5 evidence and exact baseline-failure reproductions are recorded separately; no L6/L7 or blanket CI exception. Commit/Deploy/Live acceptance of this package is pending the final receipt. Stage4B stays OPEN until that gate passes; afterwards only long-window WAL/SSD/storage monitoring and explicitly unproved historical causes remain, rather than speculative optimization tasks. Stage5 has not begun.


### 2026-10-07 — Stage 4B final delivery accepted

Source commit `7de9f88`, standard backend/docs deployment and API-only explicit restart complete. Six samples over308s retained revision69, active/provenance/exclusive, Provider member1456/binding revision4 and one Auto candidate. Health/routing/watchdog/Xray stayed in_sync; generated/mounted runtime hashes matched; provider request/discovery/mutation counters stayed0. No provider mutation, outage or forced refresh. [Live receipt](/srv/fwrouter/knowledge/audits/stage4b_completion_2026-10-07/LIVE.md).

This dated acceptance supersedes the earlier release-pending entry and historical Stage4B OPEN wording in the measured scope. Remaining WAL/SSD/retention volume measurements are monitoring tails, not confirmed outstanding bugs. Required native observations remain; unproved old fallback/burst triggers remain explicitly unproved. Known exact-ID baseline failures remain open outside this package; no blanket exception or full CI-plan promotion is claimed. Stage4B is closed; Stage5 is the next active milestone and has not begun. No default L6/L7.

## 2026-10-07 operational latency/resource evidence supplement

Baseline `65c3024`; [durable audit](audits/operational_latency_resource_2026-10-07/REPORT.md) and [local canonical report](</решения/аудит/Operational Latency & Resource Audit 2026-10-07.md>). Audit only: no code optimization/deploy/restart/provider mutation. Keep live serial GET, isolated SQLite/component, temp-target installer and pinned network-none native validation distinct from successful production lifecycle timing.

Natural-cache summary median1.357s/max1.483s (n3), warm mixed median4.316ms; Mihomo current-config native check844ms/500msCPU/98.9MiB peak and Xray248ms/75msCPU/9.28MiB (n1 each). Normal GET provider counters delta0 in the retained bracket. Subtree/background writes are not per-request DB or physicalSSD writes. No P0 overload established.

Evidence-first future order: P1 partition summary/runtime miss and scheduling; P2 attribute repeated cleanup/deferred prewarm/logging costs before any fix; staging full restart/generation/switch/client lifecycle/resource/guard gates; long-window WAL/SSD monitoring. Require matched before/after and unchanged ownership/readback/state semantics. Stage4B accepted scope is not reopened; Stage5 remains next planned implementation and was not begun. Unmeasured live operations and historical unknowns remain explicit; no default L6/L7.

Successful isolated fake-runtime3create/3delete/alias follow-up returned empty final inventory. Twelve fake reloads are two source stages per create/delete (adapter mutation, then binding materialization); compare intermediate/final semantics/native readiness in staging before any coalescing claim. Observer calibration35.9–51.9ms prevents action-attributed CPU/RSS for tiny operations. Initial fixture failures were denied unpatched host boundaries, not a demonstrated product regression.


### 2026-10-08 — Operational Performance Fix Pass 1

Baseline `4bb2391`; [scoped evidence](/srv/fwrouter/knowledge/audits/operational_fix_pass1_2026-10-08/REPORT.md). Four measured corrections: duplicate manager cleanup, identical Settings write/prewarm no-op, summary enforcement reuse with fallback, and event INSERT readback removal. Core/revision/incarnation/exclusive/provider/fixed-Xray semantics are unchanged. Source/affected L0–L5 are tracked separately from Commit/Deploy/Live; delivery receipt will be linked after acceptance. No L6/L7, new cache/index, batching, parallelization, or lifecycle-stage deletion. One exact Xray lifecycle fixture failure reproduced on `4bb2391` remains open. Stage 4B historical closure is preserved; this operational package does not start Stage 5. Pass 2 candidates: consolidated Xray lifecycle candidate/apply design and measured safe independent-stage concurrency; bootstrap/changed-save prewarm need new evidence before changing.


### 2026-10-08 — Pass 1 delivered / bounded acceptance

Source/affected Tests/Commit `f78857f`; standard backend/docs Deploy and API restart completed. [Live receipt](/srv/fwrouter/knowledge/audits/operational_fix_pass1_2026-10-08/LIVE.md):136.8s same-process observation, revision75/member1456/exclusive/provenance preserved, unchanged generated/mounted/native config and78/78/78Xray binding semantics, provider request/discovery/mutation deltas0, no observed oscillation, finalfailedunits empty. NativeMihomo current-digest validationPASS; exact matching Xray native receipt reused. No forcedoutage/providerPATCH/clientmutation.

Affected135component+154regressionPASS; one exact baseline-only Xray fixture failure retained. IsolatedL4PASS; formal live-smoke helper rejects1.65MB `/state/system` due unchanged32KiB response cap, so complete formal tool gate remains open. Separate manual readonly parity/state/native evidence passed. A collector timer overlapped45sAPI startup, failed once before readiness, then recovered on its next normal tick; no unit/reset/retry policy changed. Follow-up candidates: readiness-aware collector lifecycle, compact critical-state smoke contract, consolidated Xray lifecycle after candidate/readback proof, independent-stage concurrency/resource review. No latency gain claimed for logging or Settings; deterministic removed-call counts are acceptance. Stage5 not started; do not reopen historical Stage4B findings without evidence.


### 2026-10-08 — Live operational and data-plane benchmark (baseline 6ba7f6f)

[Detailed live evidence](/srv/fwrouter/knowledge/audits/live_operational_dataplane_2026-10-08/REPORT.md); [local report](</решения/аудит/Live Operational + Data-plane Benchmark 2026-10-08.md>). Measurement-only milestone: no application optimization, routing-policy redesign, provider PATCH, forced outage or Tailscale/rescue restart. Separate real accepted-job, effective/native and terminal-readback timestamps; safe analogues and source tracing do not close unmeasured live gates.

One real owned Xray client create: accepted1.113s, terminal verified23.642s; two short SIGTERM restart cycles471/411ms and two native-test candidates. Approximately20.24s between first runtime start and second candidate creation remains internally unattributed. API-subtree co-interval27.114CPU-s,577.8MB sampled cgroup peak and22.28MB writeI/O include observer polls/background and are not pure handler or physicalSSD cost. Eleven real log records produce11commits; durability/order must precede any batching proposal.

Bounded data-plane evidence:20/20 short HTTPS requests, serialDirect/Mihomo total p50 345.1/226.7ms (n8/path), real owned Xray rawWS/publicTLSWSS216/425ms (n1/profile), sampled existing global/fixed handoffs succeed. Server-local ingress is not remoteWAN client performance. Five directICMP probes0%loss; host TCP counters cannot attribute unrelated resets. Four small transfers total1MiB; subsecond curl rate cap was not strict, so no capacity/UDP/sustained-load claim.

Future priorities only: P1 instrument the inter-stage Xray lifecycle CPU/DB/queue/projection gap before consolidated-candidate design; partition API readiness/dependency/background overlap; P2 explicit event-batch durability contract and summary cold-runtime attribution. Required safety/readback/rollback stages cannot be removed on counts alone. Parallelization remains a reviewed candidate, not implemented. Shared global switches, fullMihomo restart, changed refresh, provider-policy enabling, destructive reset/cleanup, batches and remote/UDP/sustained traffic remain explicit unmeasured/staging gates.

Source application remains unchanged; audit scripts/docs and final live cleanup/parity receipts are the commit scope. L0 artifact/static checks and necessary liveL4 readbacks only, noL6/L7. Historical Stage4B closure is preserved and Stage5 has not begun. Final deploy/restart and owned-client cleanup outcomes are authoritative in the linked receipt rather than inferred from the initial snapshot.

Live delivery addendum: exactly one same-baseline backenddeploy64.115ms/post-copy criticalreadback293.463ms; one explicit APIrestartcommand365.104ms/criticalready44613.753ms. Owned client create/alias/fixedoverride/delete finished; delete21.122s,all78existing identities/bindings/routes preserved, testidentity/privatefile removed. Finalschema24/revision75/Provider1456/provenance/exclusive unchanged,zerojobs/failedunits,generated/mountedparity. PostrestartsamePIDnormalGETprovider counterdelta0. Whole cross-restart counters are not comparable. Startuptrafficoverlap and full owned fixedWSS probe are unmeasured due observer failures, not simulated successful gates. FutureP1readiness/internalXraylifecycle attribution remains evidence-first; no fixes were started.


### 2026-10-08 — Post-benchmark prospective fix plan

Read-only source/retained evidence audit baseline `eb77ebe`; [technical plan](audits/post_benchmark_plan_2026-10-08/REPORT.md). No application/instrumentation edits, new live measurements, tests, deploy/restart or provider mutations. NoP0 demonstrated. P1 observed APIreadiness44.614s and Xraycreate/delete23.642/21.122s; internal20.24sgap attribution remains unknown. Source confirms repeated per-subject/binding projection and current two different native-test/restart stages; consolidation requires explicit candidate/evidence/recovery equivalence.

CRUD runtime_verified currently follows generated-host-file checks/reload command, not independent native-loaded proof; close this contract before lifecycle optimization, reusing existing generation/adapter mechanisms. This does not invalidate independent prior live native/parity/traffic evidence or prove a historical live failure. P2 logging11commits/11events, no-change scrub tempwrites/fsync and coldsummary attribution remain evidence-gated. Bounded readiness/lock/readback polls are not blind mutation retries; no traced unconditional20/44sdelay found. Direct/proxy latency difference has confounded DNS/upstream contours and is not a FWRouterfix finding.

Next order Package1→Package2→conditionalPackage3→Stage5, justified by measured operational cost and the sourceproof boundary. Historical Stage4B closure remains intact. All futurefix Source/Tests/Commit/Deploy/Live gates remain OPEN; this is documentation planning only. AffectedL0–L5 percontract; no defaultL6 and L7 only disposable staging.


### 2026-10-08 — current CI-first order and operational hold

The active sequence is Test Architecture & CI Stabilization, then full application staging acceptance of the preserved Operational Performance branch, then Stage 5 and subsequent dependencies. Operational source remains on local `stage/operational-performance-fixes` at `6004400`; Packages 1–2 have implementation and bounded deployment evidence, but overall acceptance is **PAUSED/BLOCKED** pending full application Xray CRUD/crash/restart/recovery acceptance and correctness/performance review. Package 3 is unimplemented unless qualifying evidence appears. This documentation branch starts from main `e49510c` and does not contain those optimization commits.

CI uses GitHub-hosted Actions only: push L0–L1, PR affected L0–L5, main integration/native smoke, L6 nightly/manual/release-policy, L7 as an explicit compatible release gate. Unsupported required scenarios remain blockers or need a separately approved future gate. CD is deferred pending a separate decision; production remains manually deployed with the installer. No workflow/test/runtime change is implied by this dated status. The former wording is preserved in [history](history/ROADMAP_PRE_2026-10-08_CI_STABILIZATION.md).

### Phase D stopped evidence checkpoint — 2026-10-10

Phase D remains **BLOCKED**. Scoped hosted checkpoints: metadata 9/9 PASS (37963072861), provider singleton 1/1 PASS (37963610399), explicit host-observation fixtures 6/6 PASS (37965069661), eleven-case provider cohort **2 PASS / 9 FAIL** (37965465706). Fresh numeric zero-delay evidence explains only part of the probe failures; recovery/CAS/browser acceptance is still open. Last full results are not superseded by scoped runs. Additional read-model fixtures and fence diagnostics are source-prepared, **NOT RUN**. No Phase E/CD, PR/merge, production deployment or runtime changes. See `knowledge/audits/test_architecture_phase_d_2026-10-09/REPORT.md` and `PROVIDER_COHORT_CHECKPOINT_37965465706.json`.

### Phase D strict native evidence and environment plan — 2026-10-10

Phase D remains BLOCKED. Adapter zero-delay normalization is corrected in source with strict same-URL/fresh native evidence; scoped isolated L1 passed 5/5 (37969825404). Eighteen explicit host-independent read-model/isolation nodes passed (37971905644). Recovery-controller, browser subject-ID and actual post-PUT CAS test preconditions are corrected in source, but their hosted acceptance is pending. Full functional 23/37 and affected 1112/1241 receipts are not superseded. Historical seven exact IDs are not yet re-confirmed together on this head.

Target environment: GitHub-hosted Ubuntu host, existing isolated Compose, Debian 12 userland, pinned immutable dependency inputs. Source-free GHCR dependency images plus fresh per-commit source build are planned after current blockers; GHCR is not implemented/accepted. Trusted publication is separate from untrusted PR consumption; no package-write credentials or production secrets in tests. Cold image 1.96 GB / build 45.328 s is measured; warm/cache benefit and aggregate peaks remain open. Ubuntu kernel/host dataplane/systemd parity is not implied. L7 remains separately manual/release-gated and NOT RUN. Phase E/CD and return to operational work remain deferred. See the Phase D REPORT and TARGET_CHECKPOINT_37970549010.json; source changes are not deployed.
