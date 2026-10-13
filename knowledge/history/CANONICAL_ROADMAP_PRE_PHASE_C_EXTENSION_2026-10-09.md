# FWRouter — canonical active roadmap

Updated 2026-10-08: Test Architecture & CI Stabilization is active. Phase A audit was delivered at `38345d4`; Phase B test-only refactoring and bounded isolated verification are ACCEPTED after final engineering review in bounded source/local-test scope in `stage/test-architecture-ci-stabilization`; 4/11 historical failures now pass and 7 remain explicitly unresolved. Phase C/D/E and remote CI acceptance are not complete; CD remains deferred. Operational Performance Fixes remain PAUSED/BLOCKED on the unchanged `6004400` branch pending full application acceptance. Superseded plan-only status wording is preserved in [Phase B history](/srv/fwrouter/knowledge/history/TEST_STABILIZATION_PHASE_B_2026-10-08.md).

## 2026-10-08 — Phase C source checkpoint; acceptance BLOCKED

Phase C implements the isolated hosted-native-process harness and application/browser/crash test sources in the existing milestone branch. Local pure isolation/gate contracts are separate from native acceptance. Native/application/browser/L7 scenarios are **NOT RUN** on production; hosted qualification and missing joined coverage remain open. See [Phase C report](/srv/fwrouter/knowledge/audits/test_architecture_phase_c_2026-10-08/REPORT.md). Phase C is not ACCEPTED; Phase D/E are not started. Seven historical failures remain unresolved. Operational `6004400` is preserved and PAUSED/BLOCKED. No application, runtime, workflow or deployment changes. Previous wording is preserved in [pre-Phase-C history](/srv/fwrouter/knowledge/history/CANONICAL_ROADMAP_PRE_PHASE_C_2026-10-08.md).

## Правила evidence и статусов

Разделяйте **Source / Tests / Commit / Deploy / Live**. Не закрывайте runtime gate только на основании source/tests; не называйте исторический audit актуальным evidence без повторного наблюдения. Архивированные версии и detailed checkpoints доступны в [history](history/README.md). Неизвестная причина остается `UNPROVEN`; не выводите её из совпадения по времени.

Для новых verification results сохраняйте воспроизводимую команду, commit, baseline/revision, test environment, pass/fail/skip, exact novel/baseline classification и redacted durable report в `knowledge/audits/`. Временные `/tmp` файлы сами по себе не являются durable evidence. Не скрывайте raw baseline failures.

Production-деструктивные тесты не выполнять. Fault injection и failure/recovery приемка принадлежат disposable staging и не должны затрагивать production. Rollback Xray generation code-only: сохранять текущие SQLite intent и runtime; не восстанавливать старую DB/checkpoint/config поверх verified current generation и не replay-ить старые writes вслепую.

## Текущие completed checkpoints

| Область | Source / Tests | Commit | Deploy | Live / границы |
|---|---|---|---|---|
| Xray stale-generation current-state recovery и deferred publication | Historical raw run: 1,322 passed, 52 failures with the same exact IDs as the `58e053a` comparison, 1 skipped; relevant cohort 603 passed, 23 failures, 1 skipped. These are not approved CI exceptions. Separate pinned Mihomo native 20 PASS, actual Xray 26.2.6 archive/readback gate PASS, public-wrapper lifecycle/idempotence PASS | `001c6e9`, `95f54c1`, `5728d85`, `a139e66` | Standard backend/docs workflow; API startup выполнял owned recovery | Текущий 78-identity generation, receipt/readback/marker closure и стабильность подтверждены; no-op refresh verified. Измененный input live не форсировался, initial Oct 2 exception остаётся UNPROVEN. Один historical failed subscription-refresh unit предшествует deployment и не менялся; следующий natural run не наблюдался. [Report](/srv/fwrouter/knowledge/audits/xray_generation_recovery_2026-10-04/REPORT.md) |
| Provider-managed per-source credentials/bindings and active-account acceptance | Source, targeted/native coverage, current-account discovery/enable/apply/readback evidence | `9a151bd`, `9b30d98` | Deployed | API availability is not proof of remote VPN health. Step 2 correction is complete in its bounded scope; Stage 9 fault/PATCH/re-entry acceptance remains open. |
| Eight-profile Protocol Adapter and controlled current-account live acceptance | Source/native synthetic matrix; live Hy2 endpoint verified, seven other provider handshakes remain open | `dbf2afe`, `e9a90d8`, `f5674ca` | Deployed | Per-entry dispatch/common normalization supported; account variants/remaining handshakes are not live-verified. |
| Exclusive VPN-auto source intent and localized projection | Targeted source/UI evidence; explicit live enable/idempotent reconcile | `138010c`, `8339007`, `499a26b` | Deployed | Fixed Xray choices/ordinary sources preserved; exhaustion/failure is not forced in production. |
| VPN-auto selection concurrency correction | Source/isolated tests; baseline failures separately classified | `b8bcb9d`, `58e053a` | Deployed at `58e053a` | Scoped live selection stability подтверждена; historical SSH/WAN вопросы ниже остаются открыты. |

The Xray gate above закрывает только recovery уже committed current generation. Наблюдаемая запись receipt и отсутствие checkpoint — evidence для именно этого состояния, не универсальное доказательство changed-input apply.

Дополнительные point-time live facts: Health HTTP 200, schema 23, `problem_count=0`; API и gateway running, Mihomo/Xray active, оба runtime-контейнера running. Seven public profiles содержали 70 unique identities, все входят в native-loaded set 78. Owned selection revision перешел 11→15 без смены active/provenance; provider binding/member/revision остались прежними. Provider request/mutation counters были zero после restart, а pre-restart cumulative value 1 не атрибутирован и счетчики reset; это не lifetime-zero claim.

## Единственный текущий execution order

Активен только этот порядок. Датированные записи ниже — история evidence на указанный момент, а не текущий план. Прежние формулировки сохранены в [history](history/README.md), включая [точный canonical snapshot до этой перестановки](history/ROADMAP_PRE_2026-10-08_CI_STABILIZATION.md).

1. **Test Architecture & CI Stabilization — активный milestone.** Phase A delivered: `38345d4`; Phase B ACCEPTED in bounded source/local-test scope: safe bootstrap, test/fixture corrections, dependency-complete selection and bounded local verification. [Phase B report](/srv/fwrouter/knowledge/audits/test_architecture_phase_b_2026-10-08/REPORT.md); 4/11 historical failures PASS, 7 unresolved; qualified process/native and full-application gates remain OPEN. [Phase A report](/srv/fwrouter/knowledge/audits/test_architecture_phase_a_2026-10-08/REPORT.md). Phase C acceptance remains BLOCKED; Phase D/E remain OPEN and require separate user authorization; no workflows, push, remote runs, CD or production deployment are implemented in Phase B. Existing Foundation `178b471` remains a historical completed Source/Tests/Commit checkpoint. GitHub-hosted only; no self-hosted runner/project VM.
2. **Operational Performance Fixes — PAUSED/BLOCKED.** Сохранить локальную `stage/operational-performance-fixes` на `6004400`; её коммиты не переносить в milestone-ветку CI. Packages 1–2 содержат реализованные изменения и bounded deploy evidence, Package 3 не реализован и остаётся условным. PR/merge запрещены до полноценного application staging acceptance. После принятия CI milestone через обычный review процесс вернуться к operational ветке, интегрировать получившийся актуальный `main`, выполнить полный обязательный staging CRUD/crash/restart/recovery acceptance, затем проверить correctness и сопоставимую производительность. Только после PASS закрыть milestone и разрешить отдельный PR/ручной merge. При провале сохранить BLOCKED и точный список gates.
3. **Stage 5 — configuration and persistence contracts.** Начать после принятого Operational Performance Fixes. Зафиксировать env/SQLite intent/generated artifacts, installer, clean install, backup/restore/rollback и поддерживаемые upgrade/release contracts.
4. **Database Architecture & Integrity Audit.** Таблицы/relations, canonical/derived/cache данные, constraints, indexes/scans, stale/orphan rows, transactions/WAL/CAS, writes/growth/contention, migrations и trigger candidates.
5. **Evidence-based Database fixes.** Только проблемы, доказанные аудитом; Core/runtime/network/selector/recovery/Health/provider/Mihomo/Xray ownership не переносить в triggers.
6. **Stage 6 — dead/obsolete compatibility and security/data-handling cleanup.**
7. **Stage 6A — measured internal execution cleanup.** Использовать Stage 4 evidence; не создавать независимый performance backlog.
8. **Stage 8 — Core ↔ Modules contracts** по ownership, API/events/config/lifecycle/Health.
9. **Stage 7 — physical module extraction** только после Stage 8.
10. **Post-extraction functional/architecture audit.**
11. **Hardening and final runtime/config/deployment stabilization.**
12. **External Telemetry Ingestion / Metrics and Traffic Accounting — поздний отдельный milestone** после extraction и DB/architecture stabilization. FWRouter экспортирует только domain evidence; host/container metrics остаются отдельному observability project. Не готовить future-version schema/analytics заранее.
13. **Final UI redesign.**
14. **Production Documentation**, включая полную техническую документацию принятой test/CI системы.
15. **Stage 9 — disposable staging failure/recovery validation.**
16. **Stage 10 — final release audit.**

**CD не входит в текущий execution order и отложен до отдельного решения.** До отдельного решения production deployment остаётся ручным через существующий installer: `/srv/fwrouter/installer/install.sh --deploy --component backend --component ui` (при изменении документации дополнительно `--component docs`). Этот режим копирует выбранные компоненты и сам не перезапускает сервисы. Когда backend менялся и требуется применить его в работе, API перезапускают отдельно через `systemctl restart fwrouter-api.service`; docs-only deploy перезапуска не требует. Будущий CD — самостоятельный проект с protected environment/approval, exact-commit promotion, readiness/Health/native parity/smoke и проверенным rollback; GitHub Actions пока не должны автоматически развёртывать production.

## Test policy для каждого следующего implementation milestone

Отчёт перечисляет фактически запущенные уровни и причину. По умолчанию — affected L0, affected L1/L2 и необходимые L3/L4; L5 при изменении shared/domain contracts. L6 не запускается по умолчанию, только по scheduled/manual/milestone/release/policy gate. L7 — отдельная staging/release acceptance после подтверждения совместимости выбранного изолированного окружения. Не объявлять gate пройденным по плану, dry-run или неполному выборочному прогону.

## Stage 4 — Performance & Resource Efficiency Audit

Deliver a read-only measured baseline for the current version before proposing optimization: CPU/RAM; API latency; SQLite query latency/count; WAL/DB I/O; SSD writes and storage growth; polling, timers, jobs and logging volume; payload/request duplication; process/container overhead; background wakeups; and adapter/runtime cost. Record exact workload, host/runtime versions, resource ceilings, command and limitations. Do not treat a missing sample or unstable host as a measured bottleneck. Any subsequent fix must include comparable before/after measurements. Preserve a minimal CPU/RAM/SSD footprint as an architectural invariant.

## Stage 4B — Performance & Resource Fixes — completed (2026-10-07)

Accepted measured scope includes earlier schema/read-model/log-retention/applied-status batches, the `49591c2` scoped-bindings fix, and completion package `7de9f88`: command-specific maintenance admission, native counter read aggregation, and same-operation identical-candidate validation reuse pinned to an immutable local image ID. Source/affected Tests/Commit/standard backend+docs Deploy/bounded Live are complete. See [completion report](/srv/fwrouter/knowledge/audits/stage4b_completion_2026-10-07/REPORT.md), [test receipt](/srv/fwrouter/knowledge/audits/stage4b_completion_2026-10-07/SOURCE_TEST_RECEIPT.md) and [live receipt](/srv/fwrouter/knowledge/audits/stage4b_completion_2026-10-07/LIVE.md).

Before/after: counter native bundle median289.2→68.1ms (n3; full new projection80.1ms); direct-route summary1534.5→1351.9ms (n1 each; 76 SQL statements and7403B payload retained); nft subprocesses9→5. Identical transition/final candidates reuse one required native validation rather than running two (~720ms per check including Docker startup). Required local validation, distinct-candidate native validation, final apply/readback/CAS and last-good remain intact. No new persistent cache, index, DB schema, routing/provider policy or timer cadence change.

Live: six samples over308s, revision69, same Provider member1456/binding revision4, active/provenance/exclusive unchanged, one eligible Auto target; Health/routing/watchdog/Xray in_sync; mounted/generated Mihomo/Xray hashes match; provider requests/discoveries/mutations remain0 on the new process. No outage, PATCH, forced refresh or switch. This is bounded observation, not Stage9 fault or protocol-handshake acceptance.

Only long-window WAL/device writes, SSD/storage growth and retention rewrite volume remain as monitoring tails; no current write-amplification bug was proved. Required residual native observation cost is retained. Historical burst scheduling/fallback/health triggers remain UNPROVEN, not newly confirmed regressions or active speculative fixes. Exact prior priorities and OPEN wording are preserved in [history](history/ROADMAP_PRE_2026-10-07_STAGE4B_COMPLETION.md). At that 2026-10-07 checkpoint Stage 5 was next; the current CI-first sequence above supersedes that dated order.

## Stage 5 — Configuration and persistence contract

Map environment/config files, SQLite persistent user intent, observed provider/runtime state, generated artifacts and last-good/recovery snapshots. Define startup/apply ownership and versioning; clean-install and supported-upgrade behavior; backup confidentiality/retention/access; restore and code-only/runtime rollback semantics; installer defaults and migration evidence. Keep generated/observed state from becoming user intent.

## Database Architecture & Integrity Audit and fixes

Audit field ownership and canonical-versus-derived/cached duplicates; foreign/unique/check constraints; orphan detection; indexes/full scans; transaction boundaries, WAL, revision/CAS; write amplification/growth/contention; migration debt and trigger scope. For each candidate, classify pure integrity invariants separately from Core/runtime decision logic. Fix only evidence-backed DB-layer defects, validate migration/rollback on isolated data, and reject triggers that depend on external/network/selector/recovery/health/provider or Xray/Mihomo apply state.

## Provider contract status and correction target

Provider records, credentials, available configs, members, observations and applied state are source-scoped by `source_ref`; the current schema does not impose a singleton provider binding. Provider data is not persistent user intent for Core selection. Exclusive VPN-auto is a separate operator intent. The former singleton/env credential wording remains in exact history and must not be treated as the active architecture.

Emergency Direct/re-entry correction is implemented in source and covered by targeted L0–L5 gates; commit `30a43a0`, standard backend/docs deployment, and the bounded safe live-verification scope are complete. Production real-fault, Provider PATCH, and actual Direct/re-entry acceptance remain open under Stage 9. The pre-deploy scheduled refresh failure remains unreset and unattributed; its next scheduled post-deploy execution was not observed. Normal selector, ping, Health and UI read paths do not poll the provider. Per-operation bounded API budgets, source isolation, truthful cache freshness and last-good preservation remain required. Provider API availability does not establish remote VPN health; missing/ambiguous evidence is UNKNOWN.

## Resource and observability boundaries

Stage 4 owns one measured efficiency backlog; Stage 6A consumes that same evidence. Preserve a minimal CPU/RAM/SSD footprint and bound recurring polling, timers, jobs, logs and container overhead. A separate server-metrics project may collect host/container CPU, memory, disk and process telemetry; FWRouter must not duplicate that system collector. FWRouter may export its domain metrics and may consume observability as an external client; it does not own duplicate host/container resource collection.

## Open evidence and deferred branch

- Xray changed-input recovery live acceptance is open; the October 2 initial exception cause is unknown. The historical failed `fwrouter-subscription-refresh.service` state-change predates the 2026-10-04 deployments; the unit was not touched and its next scheduled run was not observed.
- The historical raw full suite remains non-green. Test Architecture audit stores the 52 exact historical IDs and current triage in `knowledge/audits/test_architecture_cicd_2026-10-04/BASELINE_STATUS.json`; no baseline exception is approved. Across selected post-fix runs, 41 IDs have PASS observations and 11 retain a last-failed observation; this is not a single complete post-fix rerun. Temporary `/tmp` logs are provenance only, not canonical evidence. See the durable [baseline report](/srv/fwrouter/knowledge/audits/test_architecture_cicd_2026-10-04/BASELINE_AUDIT.md) and [foundation report](/srv/fwrouter/knowledge/audits/test_architecture_cicd_2026-10-04/REPORT.md).
- A prior process metric showed a cumulative provider-request value of one before API restart; counters reset at restart and the event is unattributed. The measured post-restart verification window was zero; do not claim a lifetime zero-request audit.
- The Emergency Direct/provider correction was committed as `30a43a0` and deployed backend/docs with only the API service explicitly restarted. Safe live checks passed within the scope described in LIVE.md; no actual Provider mutation or forced Direct/re-entry was exercised. The pre-deploy scheduled `PROVIDER_MATERIAL_HANDOFF_STALE` failure was not reset and remains unattributed; the next scheduled post-deploy run was not observed. The deterministic missing-location source defect does not alone establish historical causation.
- Settings inventory tab/locale loading race is unconfirmed and requires reproduction before calling it a defect. Measure LAN activity TTL; do not extend a LAN result to Xray/external/service clients.
- External Connections CRUD/lifecycle remains unverified in isolated tests/staging; confirm remaining Rules/External Connections typed-audit coverage without adding a parallel audit system.
- Confirm the direct manual removal path for the current effective vpn-auto membership and subsequent fixed-target transition; refresh/source-delete checks do not close that separate path.
- Existing SSH 15-second timer clean-address readiness redesign and Xray gateway ↔ `fwrouter-api.service` lifecycle coupling remain open. Historical WAN HTTPS reset root cause remains unknown; no regression is demonstrated by current evidence.
- Tailscale peer admission/inventory/per-subject policy requires an evidence-based decision; offline peer state alone is not a FWRouter defect. Historical LAN activity TTL and Xray 86→85 identity cause remain unconfirmed.
- Future product branch, not this current release: remove User mode/Admin-only controls, add traffic analytics/top domains/history graphs, expand Xray accounting, or add a new traffic DB schema. Do not create architectural prework for these items; finish and release the current version first.
- Other historical follow-ups remain linked in the archived roadmap and existing audit reports. Historical report wording does not activate a new backlog item by itself.

## Dated evidence history — reference only, not the active execution order

Older dated notes below preserve status as observed at that time. Their historical “next milestone” wording is superseded by the single active execution order above.

### Dated status — 2026-10-04

This active roadmap supersedes the exact archived version linked above. The Xray recovery status is scoped to current committed projection verification; changed-input live acceptance remains open. Step 2 source, targeted tests, commit, standard backend/docs deploy, and bounded safe live verification are complete; fault/PATCH/actual re-entry acceptance remains a Stage 9 gate. L0–L5 results and limits are in linked reports; L6 full suite was intentionally not run. Existing `/tmp` logs or ID lists are not durable evidence by themselves.

### Dated rollout clarification — 2026-10-04

The earlier same-day Step 2 status was Source/Tests complete with release gates open. It is superseded by commit `30a43a0` and the bounded backend/docs deployment and safe live verification in [LIVE.md](/srv/fwrouter/knowledge/audits/emergency_direct_provider_recovery_2026-10-04/LIVE.md). This clarification closes those delivery gates only; it does not close Stage 9 real-fault, remote PATCH ambiguity across process crash, or production Emergency Direct/re-entry acceptance. The natural pre-deploy scheduled refresh failure remains unreset and unattributed, with no post-deploy scheduled run observed.

### Test foundation and documentation reconciliation — 2026-10-04

Step 3 Source/Tests/Commit is complete in `178b471`: 149-file catalog, L0–L7 taxonomy, deterministic domain/level/affected selection, exact-ID baseline classification, fixture contracts, isolated smoke and hosted CI definitions. Local infrastructure acceptance passed; no product full suite or L6 run was performed. Across selected baseline observations, 41 exact historical IDs have PASS evidence and 11 retain a last-failed observation; there are no approved baseline exceptions and no single complete post-fix 52-ID rerun. Remote workflow execution, locked-environment acceptance, native runtime provisioning, deploy-authorization integration and disposable L7 remain open. No production deploy/restart was part of the milestone. This documentation reconciliation is complete; Stage 4 Performance & Resource Efficiency Audit is the next active milestone. The superseded active wording is preserved in [dated history](history/ROADMAP_PRE_2026-10-04_TEST_CICD_RECONCILIATION.md).

### 2026-10-05 — functional correction before Stage 4

The operator requested the narrowly scoped automatic-switch policy/configured-membership correction above before Performance. Earlier sequencing is preserved exactly in [history](history/ROADMAP_PRE_2026-10-05_AUTO_SWITCH_POLICY.md). Stage 4 remains the next audit after the functional correction; no closed provider/protocol/recovery milestone is reopened.

### 2026-10-05 — policy/configured-membership acceptance

Source/affected Tests/Commit complete in `7478b02`; standard backend/UI/docs deploy with API-only explicit restart completed. Schema 24 defaults automatic member switching OFF. Safe live evidence: member 1456/current=applied, binding revision 4/4, Auto revision 31, exclusive/provenance/effective pool unchanged; ordinary Auto edit→restore persisted with no runtime apply/reselect; provider requests zero. RU/EN 1440/390, no overflow/JS errors; native Mihomo/Xray validation and generated/mounted parity PASS, 78/78 bindings preserved. Browser harness failures are documented separately and do not claim captured PATCH output. Real outage/re-entry/remote mutation acceptance remains Stage 9, not induced here. Next active milestone: Stage 4 Performance & Resource Efficiency Audit.

### 2026-10-05 — Stage 4 bounded audit checkpoint

Read-only Stage 4 report: [аудит №1](</решения/аудит/аудит №1 2026-10-05.md>); durable source-owned [evidence summary](/srv/fwrouter/knowledge/audits/performance_resource_2026-10-05/REPORT.md). Baseline `0b24cd1`, deployed application `7478b02`, schema24. Source: audit/evidence only; Tests: bounded read-only measurements, isolated microbenchmarks and L0 artifact checks; Commit: documentation/evidence checkpoint; Deploy: none; Live: bounded reads, no fault acceptance. No application fixes or provider mutations.

No P0 confirmed. Confirmed: inventory Health-before-role admission (cold narrow GET 2.48s vs warm 11–26ms); schema initializer on Health read (isolated 77DDL/2DML, median4.15ms); no-expiry retention temporary rewrite (synthetic2.02MB, median37.68ms). Six Admin role requests are not duplicates; sub-ms traffic scan is not an index fix. Runtime generation cost, HTTP SQL/serialization attribution, contention, background CPU/wakeups and long-term SSD/WAL/log growth remain measurement gates, not closed production acceptance.

Next: one evidence-based sequence in the report—schema read/init boundary, cold inventory attribution/scoping, residual UI burst analysis, background/member/watchdog attribution, retention no-op writes, then only proven DB/write candidates. Every implementation needs comparable before/after evidence and affected L0–L5 policy; no default L6. Stage5/DB audit/cleanup/module order unchanged. Previous active wording is preserved verbatim in [history](history/ROADMAP_PRE_2026-10-05_PERFORMANCE_AUDIT.md); the earlier “Stage4 next” dated entries are historical, superseded by this checkpoint.

### 2026-10-05 — UI performance supplement / Stage4B

The UI supplement adds browser/TTFB/download/render/heap evidence and source-attribution boundaries at baseline `6ef940b`; it does not fix or deploy application behavior. Stage4B is now the explicit next active milestone; Stage5 follows its closure. Former unnamed measured-fixes wording and initial Stage4 priorities remain preserved in [pre-supplement history](history/ROADMAP_PRE_2026-10-05_UI_PERFORMANCE_4B.md) and the original dated audit; the Stage4B section is authoritative for forthcoming fix scope. Unproven root causes remain unproven.

### UI evidence addendum — complementary Settings observation

A single supplementary Settings document flow on `6ef940b` recorded an ambiguous initial empty/error journal marker at 8.669s (not proven usable content or full workspace readiness), cached journal tab returns 305/368ms and locale change 314ms with zero new requests. Completed static ResourceTiming bytes 2.36MB (background PNG 1.28MB, loopback download 8.4ms) are a P2 footprint review candidate, not a proven multi-second latency cause or permission to redesign/remove assets. Full workspace completion, true warm HTTP page cache and remote/mobile performance remain open. [Evidence](/srv/fwrouter/knowledge/audits/performance_resource_2026-10-05/UI_SETTINGS_COMPLEMENT.json). Stage4B includes contract-safe payload/asset review only after attribution and requires before/after; priority order and Stage5 dependency remain unchanged.

### 2026-10-05 — Stage4B first measured fix batch

Source baseline `24ef1ba`; [English implementation/attribution evidence](/srv/fwrouter/knowledge/audits/performance_fixes_2026-10-05/REPORT.md). Cold narrow inventory is predominantly canonical runtime enforcement, not SQL/serialization; full Health freshness and existing cache single-flight are preserved. Workspace 4.48–4.65s is predominantly technical JSONL reading/sanitization (4.29–4.48s over approximately12.6MB), establishing a backend cause for this path. Browser `/servers` burst/scheduling and background overlap causal attribution remain open.

Three minimal fixes: read-only schema inspection rather than initializer on Health reads; bounded newest-log selection before recursive sanitization, preserving ordering/filters/redaction; no temporary JSONL rewrite when no records expire. No frontend/Auto-sorting fix, speculative index/cache, routing change, provider workflow or runtime apply is introduced. New tests and affected cohorts are recorded with exact baseline reproduction; eight selected historical failures remain visible and are not a blanket CI exception. No L6 or destructive L7.

Commit/standard backend-docs deployment/safe Live evidence are separate delivery gates. Stage4B remains active for residual measured gates; Stage5 is not activated by completion of this first batch.


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

At the 2026-10-07 checkpoint this dated acceptance closed Stage4B in the measured scope; its then-next Stage5 wording is superseded by the current CI-first execution order. Remaining WAL/SSD/retention volume measurements were monitoring tails, not confirmed bugs. Known exact-ID baseline failures remained open with no blanket exception or full CI-plan promotion; no default L6/L7.

### 2026-10-07 — Operational Latency & Resource Audit (baseline 65c3024)

[Operational report](</решения/аудит/Operational Latency & Resource Audit 2026-10-07.md>); [durable English evidence](/srv/fwrouter/knowledge/audits/operational_latency_resource_2026-10-07/REPORT.md). Measurement/attribution only: no application fixes, production deploy/restart, forced refresh/switch/outage or provider PATCH. Temp-target standard installer, private SQLite/component fixtures, bounded live serial GETs and network-none pinned native validators are separate evidence classes; none establishes live mutating-operation completion.

Measured: summary spaced natural-cache median1357ms/max1483ms (n3), warm mixed cohort median4.316ms (n5); servers limited median58.7ms, selector readonly253.4ms, events186.8ms (n5 each). Current Mihomo native validation844ms/500msCPU/98.9MiB cgroup peak; Xray248ms/75msCPU/9.28MiB (n1 each). Installer copy≤60ms is not service readiness. Live co-interval subtree CPU/writes include natural background and are not request-only or physicalSSD attribution. Normal GET provider counter delta0 in the retained bracket; historical targeted configGET count1 is not a lifetime-zero claim.

Future priorities only (not authorization to implement):

- **P1 measurement gate:** partition summary/cache-miss/runtime enforcement into queue/handler/DB/native/serialization and capture background overlap before another fix. No new cache/index justified here.
- **P2 attribution gate:** repeated stale-job cleanup, Settings deferred prewarm and logging commit/projection cost; prove redundancy/frequency/durability semantics before minimizing work. Warm bootstrap DDL/upserts alone do not imply a high-frequency production bug.
- **Staging operational gate:** full API/Mihomo/Xray restart/readiness, changed/no-op generation/refresh, routing/fixed-target switches and successful Xray client batch lifecycle with simultaneous resource peaks, lock wait and accepted/effective/verified timestamps. Source/native/mock timing cannot close these live gates.
- **Monitoring tail:** production WAL/fsync/physicalSSD amplification, storage growth and full background cycle load. Fast HTTP alone is not a low-resource claim.

Every future confirmed fix requires matched before/after latency, CPU/RSS/I/O/counts and unchanged outputs/intents/fences; affected L0–L5 only per contract, L6/L7 only their gates. No P0 overload proven. No stage-order change: prior Stage4B measured acceptance remains complete; Stage5 remains the next planned implementation milestone, not implemented by this audit. Historical findings/receipts stay intact.

Client lifecycle supplement: isolated fake-runtime3create/3delete/alias PASS, final fixture inventory0. Twelve fake reloads map to initial adapter mutation plus binding materialization (two stages per create/delete); equivalence/redundancy is not proved. Add intermediate/final config and native readiness comparison to the staging operational gate, not an immediate reload-removal task. Small-operation sampler calibration35.9–51.9ms makes fast-path attributable CPU/RSS unavailable; reported cohort pressure includes observer. No new product regression inferred from earlier denied fixture boundaries.


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


### 2026-10-08 — Post-benchmark audit and prospective fix sequence

Audit baseline `eb77ebe`; [English technical plan](/srv/fwrouter/knowledge/audits/post_benchmark_plan_2026-10-08/REPORT.md), [local synthesis](</решения/аудит/Post-benchmark audit and fix plan 2026-10-08.md>). Documentation/source inspection only; no new live probes/tests/application changes/deploy/restart. No P0 overload proven. P1:44.614sAPIready and23.642/21.122sXrayCRUD with high observed co-interval resource cost;20.24sinterstage cause remains UNPROVEN. Source-confirmed per-client projection and repeated collector work are candidates, not measured savedseconds. Two current distinct candidate tests/restarts are not declared removable before canonical candidate equivalence.

New P1 proof prerequisite: CRUD source checks generated host config after successful compose restart, then reports runtime_verified; independent native-loaded proof is missing in that path. No actual live mismatch shown; prior separate native/parity/traffic evidence stays valid. Reuse existing generation/adapter fencing/native mechanisms, do not invent parallel ownership. P2:11events/11commits, unchanged scrub tempwrite/fsync, cold summary attribution. Bounded readiness/lock/readback polling is not classified as blind mutation replay; no unconditional20s/44sdelay found. DIRECT/proxy difference does not establish FWRouter overhead or justify DNS/routing changes.

Execution: Package1→Package2→conditionalPackage3→Stage5. This explicitly justified insertion uses newly measured operational latency/resource and proof findings; historical Stage4B completion is preserved. Parallelization only future conditional immutable read/validator candidate with resource budget; common writers/finalCAS remain serialized. Source/Tests/Commit/Deploy/Live of fixes all OPEN. Current checkpoint is documentation only; no implementation acceptance claimed.


### 2026-10-08 — Operational Performance Packages 1–2 delivery reconciliation

Package 1 Source/affected Tests/Commit: `69bc636`; Package 2: `3d285c3`; bounded delivery-evidence follow-ups are retained through `6004400` on local `stage/operational-performance-fixes`. Standard backend/docs/host deployment and bounded API restart/native/normal-read checks passed. The retained branch records 78/78 generated/mounted/native Xray parity, stable Provider/exclusive/provenance state, zero same-process normal-path provider request/discovery/mutation deltas, and no observed oscillation in the sampled window. The expanded affected run recorded 238 passes and three failures reproduced on clean baseline `e49510c`; it is not an L6/L7 result. These branch-scoped reports/artifacts were not copied into the main-based CI milestone branch. Full application CRUD/crash/restart/recovery acceptance remains open; Package 3 remains conditional and unimplemented. This does not close the Operational Performance milestone or start Stage 5.


### 2026-10-08 — current CI-first order and operational hold

The active order is the Test Architecture & CI Stabilization milestone, then full application staging acceptance of the preserved Operational Performance branch, then Stage 5 and the documented dependencies. Operational source remains on local `stage/operational-performance-fixes` at `6004400`; Packages 1–2 have implementation and bounded deploy evidence, but the overall milestone remains **PAUSED/BLOCKED** until required full application Xray CRUD/crash/restart/recovery acceptance and correctness/performance review pass. Package 3 remains unimplemented unless evidence requires it. This documentation branch starts at `e49510c` and contains no operational optimization commits.

CI plan: GitHub-hosted Actions only; push L0–L1, PR affected L0–L5, main integration/native smoke, L6 nightly/manual/release-policy, and L7 only as an explicit compatible release gate. Unsupported required staging scenarios remain blockers or require a separately approved future gate. CD is deferred until a separate decision; production deployment remains manual through the installer. No workflow/test/CI/CD implementation, deploy, restart, or provider mutation is part of this roadmap update. Superseded exact wording is preserved in [the canonical snapshot](history/ROADMAP_PRE_2026-10-08_CI_STABILIZATION.md), [the English mirror snapshot](/srv/fwrouter/knowledge/history/ROADMAP_PRE_2026-10-08_CI_STABILIZATION.md), and [the former CI map](/srv/fwrouter/knowledge/PROJECT_MAP/history/CI_STABILIZATION_AND_GATED_CD_PRE_2026-10-08.md).
