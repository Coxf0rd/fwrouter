# FWRouter — active roadmap

## Operational Packages2/3 implementation — current source checkpoint 2026-10-16

PR15 remains DRAFT. Package2 source was qualified at526221c by fullCI38826004751 (1679/0/0), but user explicitly authorized actual Package3 implementation afterward; closure-by-DEFER was corrected. Package3 now implements no-change JSONL output elimination with mandatory full scan and source-drift checks, and existing-helper/chunked Xray subject+override projections. Independent Package3 QA105PASS. Changed-file scrub CPU and batch transient-memory overhead are explicitly measured in the [implementation report](/srv/fwrouter/knowledge/audits/operational_package23_2026-10-16/PACKAGE3_IMPLEMENTATION.md). Conditional chmod and unsupported extra native/I/O candidates remain DEFERRED.

PR14 was user-merged as2d53daf while implementation was underway. Its newer roadmap/history are retained; feature branch integrates that main without rewriting history. Final combined-head full required PRCI is pending. Package2/3 are not deployed; production remains3f96e1e. Core/native proof,CAS/fencing,durable identities,SQLite durability,fail-closed and unrelated services are unchanged. No deployment,Stage5 or blanket Operational milestone closure follows from this source checkpoint.


Canonical: `/решения/roadmap/fwrouter/ROADMAP.md`. Relative links are resolved from the Git mirror `knowledge/ROADMAP.md`.

## Current state — reconciled 2026-10-16

PR11 ccc576c, PR12 baafbf2 and PR13 3f96e1e are MERGED. Production is exact `3f96e1e73be0e0fc25208a406ecc9ff259f6480f`; Package1 ACCEPTED in bounded deployed/live scope. CI milestone CLOSED; CD DEFERRED. API/Gateway/workers/timers active,78 original identities and27 protected host files retained; Core/native parity,DNS/Rescue/Tailscale and observed fail-closed state preserved. [Current evidence and risks](audits/operational_roadmap_legacy_2026-10-16/REPORT.md).

Production rollout47/47 health over460.034s,CPU27.589% of one core,RSS673.54→727.16MiB,max917.91MiB,health p50/p95/max5.098/272.4/718.239ms,newOOM0. Follow-up at API age4816s: samePID,6/6 healthy,RSS≈1.22–1.31GiB,HWM≈1.45GiB,cgroup memory.current≈1.51GiB,OOM counters0. Follow-up endpoints/lifetime counters are not an hour-long continuous soak; increased retained memory is an open observation, not a proven leak or Package1 causality. No critical regression proven; long-term resource risk stays explicit.

Synthetic Functional PASS and Production State PASS only in performed scope. External Client E2E NOT RUN; complete Debian Host/reboot/rollback NOT RUN (Stage9), full release/overall Production Acceptance remains BLOCKED on separate applicable release/client gates. No retrospective gate waiver. Public upstream credential overlap remains UNKNOWN origin with separate approved plan; final exposure/rotation disposition remains pre-public-release, no value published or rotation performed.

## Historical checkpoint preservation

Exact pre-reconciliation [canonical](history/operational_reconciliation_2026-10-16/CANONICAL_BEFORE.md) and [mirror](history/operational_reconciliation_2026-10-16/MIRROR_BEFORE.md) preserve pending-CI/PAUSED/held-rollout wording and links as dated history. Current state above and the active functional plan below supersede those statuses only within stated evidence. Pending patches are preserved byte-for-byte and manually reconciled, not applied in succession. Historical reports/snapshots are unchanged.

## Active execution order

Keep the existing sequence. Package1 is accepted in bounded source/CI/deployment/live scope. Remaining Operational packages require separate authorization; this documentation does not start them.

1. **Test Architecture & CI Stabilization — CLOSED.** Preserve accepted scope and real event evidence.
2. **Operational Performance Fixes — IN PROGRESS; Package1 ACCEPTED in bounded scope.** Current-code audits complete within declared limits. Package2 PLANNED, Package3 CONDITIONAL; neither authorized for implementation. Preserve6004400; new packages are not historical package numbers.
3. **Stage 5 — Configuration and Persistence Contracts.** After accepted Operational Performance Fixes.
4. **Database Architecture & Integrity Audit.** After supported config/persistence contracts.
5. **Evidence-based Database fixes.** Only proven audit findings.
6. **Stage 6 — dead/obsolete compatibility and security/data-handling cleanup.**
7. **Stage 6A — measured internal execution cleanup.** Reuse the single resource-efficiency backlog; plan server-wide orchestration here, gate physical migration on stable interfaces.
8. **Stage 8 — Core ↔ Modules contracts.** Optional/replaceable runtimes, instances, connections, ownership/API/events/config/lifecycle/Health.
9. **Stage 7 — physical module extraction.** Only after Stage8 contract acceptance.
10. **Post-Extraction functional/architecture audit.** Only after physical extraction. The Reference Architecture study and External Integration Framework/SDK subitems open only after that audit is complete; no vendor adoption is required.
11. **Hardening and final runtime/config/deployment stabilization.** Includes measured DNS reliability and approved orchestration work after its dependencies.
12. **Metrics Ingestion Optimization / External Telemetry and Traffic Accounting.** Late, after DB/Core/Modules stabilization; not duplicate host observability.
13. **Final UI/UX.** Do not introduce future analytics/schema prework now.
14. **Production Documentation.** Mature RU/EN public presentation and accepted CI engineering guides.
15. **Stage 9 — Recovery/Staging.** Configuration/Security Matrix, safe orchestration qualification and separate Debian Host Acceptance.
16. **Stage 10 — Release/Production Readiness.** Final audit, approved server cleanup, independently authorized deployment and mandatory Production Live Acceptance.

CD remains outside this sequence until a separate decision. No next milestone starts automatically from a CI PASS. Existing standard installer deploy mode copies explicitly selected components and does not itself restart services; a reviewed component-specific plan must protect live guard/config ownership. No generic command here authorizes a production operation.

## Test Architecture & CI Stabilization — CLOSED; ongoing event policy

PhaseA/B completed, PhaseC completed in agreed scope, PhaseD/E accepted. The earlier PR5 PRE-MERGE/BLOCKED/main-pending wording is historical and is preserved in snapshots/reports. Wrong third-party PyPI `backend` packaging was replaced by content-verified local `fwrouter-api`; GHCR publisher/independent digest-pinned fresh-source consumers and real isolated Network Testbed were verified in the agreed scope. Independent native proof, packet captures/counters, CAS/fencing, cleanup and false-green rejection remain required.

| Event | Required policy |
|---|---|
| Feature branch push | Fast affected L0–L1 |
| PR to main | Affected L0–L5 with selected native/browser; final explicitly marked trusted PR uses full mandatory safe L0–L5 |
| Actual main push | Full mandatory safe L0–L5, exact-source packaging/integration/native smoke as required by workflow |
| Nightly/manual/release policy | Separate L6 full regression; record actual run, not inferred PASS |
| Authorized manual/release | Separate qualified L7 failure/recovery; never automatic production fault injection |
| Dependency image changes | Trusted GHCR publisher; read-only digest-pinned consumers with current checkout code |
| Diagnostic branch | Explicit targeted diagnostics without unnecessary duplicated expensive tests |

Stable required check: `Affected L0-L5 with hosted profiles`. Required proof missing/FAIL/CANCELLED/TIMEOUT or unjustified SKIP blocks acceptance. `ci:full-acceptance` selects all existing safe primary L1–L4 suites plus L0 and L5 anchors; it is not scheduled/manual L6 or L7. A post-merge main event can only be checked after merge and is not an impossible precondition for it. Reverify settings/event checks when changed; the last accepted policy is not a new live GitHub-settings audit by this documentation task.

## Development workflow — Git and release policy

One independent task/milestone → one feature/fix branch → one PR → one single-parent squash commit in main. Intermediate branch commits are allowed. Source/tests/docs/roadmap travel together. Required checks precede user-controlled Squash and Merge; no agent merge or direct main push. Completed head branches are deleted under the accepted auto-delete policy only after inclusion is verified; unfinished operational work is preserved. Historical retain-branch wording is superseded, not an alternate current policy.

| Repository setting | Last accepted policy; not changed here |
|---|---|
| Squash merging | ON |
| Merge commits / rebase merging | OFF / OFF |
| Automatically delete merged head branches | ON |
| Main protection | PR + strict up-to-date stable required Actions check; no ordinary bypass/direct push |
| Production auto-deploy | Absent; CD DEFERRED |

CI → PR review/required checks → user Squash and Merge → actual main verification → RC → applicable release/manual/host acceptance → separately approved deployment → Production Live Acceptance. Merge/approval is not deploy authority. `/AGENTS.md` is not edited in this task.

## Test policy for subsequent implementation milestones

Use [Test Architecture contract](PROJECT_MAP/TEST_ARCHITECTURE_AND_CICD_FOUNDATION.md): affected L0/L1/L2 plus necessary L3/L4, L5 for shared contracts; full required PR safe scope at final promotion and every main push; L6 scheduled/manual/policy only; L7 qualified disposable/release only. Report exact Source/Tests/Commit/Deploy/Live receipts and PASS/FAIL/SKIP/NOT RUN/BLOCKED, never classify missing work as PASS.

## Operational Performance Fixes — remaining packages

P1, IN PROGRESS. Package1 ACCEPTED in confirmed source/CI/deployment/bounded-live scope. [Package1 source report](audits/operational_package1_2026-10-16/REPORT.md), [deployment handoff](audits/operational_package1_production_2026-10-16/FINAL_HANDOFF.md) and [current reconciliation/legacy register](audits/operational_roadmap_legacy_2026-10-16/REPORT.md). Historical branch `stage/operational-performance-fixes@6004400` remains evidence only; its packages1/2/3 do not identify this current sequence. No cherry-pick/replay.

| Current package | Status / priority / owner | Entry and dependencies | Completion and evidence |
|---|---|---|---|
|1 CPU / configured reads / sanitizer|ACCEPTED bounded scope; Operational,P1|PR13 merged3f96e1e, exact mainCI38804147992|1660/0/0; deployed442 backend files,78 identities,27 protected files and native/Core state preserved;47/47 health over460s,6/6 parallel reads,noOOM. Synthetic196→45.7ms and34.45MB→1.5KB; sanitizer CPU−60–87%; not measured production savings |
|2 Cache Lifecycle|PLANNED; Operational,P1; separate user approval required|Fresh unchanged synthetic baseline; inventory per-owner key cardinality/value bytes, source revision/incarnation and connection scope; no automatic implementation|Bound entry count/retained bytes, expired-entry release, bounded eviction and TTL; revision-aware invalidation, concurrent leader/follower/error behavior, multi-module/connection independence, changed/deleted configs, restart reconstruction; same native proof/CAS/fences. Matched CPU/RAM/time before/after, targeted and full required CI/native/recovery scope |
|3 DB / Native / I/O|CONDITIONAL; Operational,P1/P2|Package2 decision plus operation-owner cost evidence; each candidate root cause, effect and safety review|Explicit IMPLEMENT / DEFER / NOT REQUIRED per repeated chmod/PRAGMA, DB/native projections/readback, scrub/temp copies, redundant SSD writes/config generation and expensive health models. Preserve SQLite durability/WAL/fsync/transaction and last-good; no generic writer queue/cache replacing proof. Measure actual per-owner CPU/SQL/fsync/I/O separately from block/NAND writes |
|Long-term Resource Stability|CONDITIONAL observation; Operational,P1|Approved low-overhead natural-load plan; no stress or forced refresh|RSS/PSS/cgroup/cache cardinality, CPU peaks,daily jobs,SSD writes and health-tail attribution. Record execute/defer disposition and window. No memory-leak/SSD-wear claim from snapshots; unrun long-soak remains NOT RUN |

Package2 is ready for separate design/implementation authorization, not started. Old deep-audit A/B/C labels are historical candidate groupings: C1/C2→currentPackage1; C5→Package2; C3/C4/C6/C7 and evidence-backed others→conditionalPackage3. Startup scrub I/O was explicitly excluded from Package1. Single-flight follower deadlines require a reviewed failure/publication contract, not a silent cache optimization.

Closure requires: acceptedPackage1; Package2 decision and applicable acceptance; explicit Package3 IMPLEMENT/DEFER/NOT REQUIRED; disposition of performance-relevant legacy candidates in the single Stage6-owned register; residual CPU/RAM/SSD risks evaluated; actual comparable baseline/results; applicable staging/native/fullCI and recovery evidence; no functional/security regressions. Optional long-term work needs explicit execute/defer disposition. NOT RUN is never PASS. Only then Stage5 opens. No new stages or parallel cleanup backlog.

### Timeouts & Thresholds Audit — PLANNED, before limit changes

P1, owned by the same Operational re-audit, coordinated with Stage5 lifecycle and Stage9 host acceptance. No timeout changes are authorized here. Trace each value to introduction/history, intended failure outcome, event boundaries, environment/workload and test evidence. Separate safety/correctness gates, operational deadlines and advisory performance targets; a time budget does not replace CAS, fencing or native proof.

| Historical value | Confirmed current meaning / remaining audit |
|---|---|
|40 seconds|Advisory hosted startup engineering target; not a hard PASS assertion. PR11 startup41.4643s did not meet this target |
|45 seconds|Hard readiness ceiling in the synthetic hosted scale case, unchanged; not a universal Debian live-start SLA |
|120 seconds|Source systemd `TimeoutStartSec` operational API start budget; distinguish unit/readiness scripts and historical installed versions |
|20 seconds|Source systemd `TimeoutStopSec` operational stop budget; not a benchmark target or permission to cancel durable writers |

Sources: `tests/application_acceptance/test_runtime_scale.py`, its conftest and `host/systemd/fwrouter-api.service`; these are source facts, not a new inspection of installed unit limits. Exit: reviewed threshold register with provenance, measured p50/p95/tail/headroom, environment sensitivity, timeout/error/cleanup behavior and change justification. Preserve truthful unready/failed state and bounded shutdown; never increase a timeout solely to hide a regression. Latest Package1 hosted startup40.1817s passes45s hard ceiling but misses40s advisory by0.1817s; live journal startup26.104s is a different workload/environment. Historical PR11 values remain in evidence. No new universal SLA is invented.

## Stage 5 — Configuration and Persistence Contracts

After accepted Operational Performance Fixes: define env/SQLite intent/settings, generated/last-good artifacts, installer/defaults, supported clean-install/upgrade, confidentiality/backup/restore/rollback. Generated output is not user intent.

### Configuration Matrix & Security Acceptance — PLANNED

Priority: high release correctness; Stage5 defines supported contracts, Stage9 executes acceptance after relevant Core/config/module interfaces stabilize. This expands future coverage, not accepted Phase D scope. Use parameterized boundary cases and risk-based pairwise/combinatorial coverage; do not enumerate every cross-product. Record expected outcome, environment, exact case IDs and required proof in the future matrix before implementation.

| Scenario class | Expected contract / method | Level and conditions | Acceptance / evidence |
|---|---|---|---|
| Custom LAN IP/subnet/mask, gateway, DHCP/DNS, interface names and ports | Valid supported values persist/generate correctly; invalid/overlapping/incomplete input rejected before unsafe apply | L1/L2 boundaries; L3/L4 isolated Compose/kernel where supported | API/config/DB intent and native readback, isolated DHCP/DNS packet observations |
| Custom domains, `.test`/`.invalid`, local records, DNS capture/nft sets | Correct answers and routing; no forbidden DNS leak/loop | L2 contracts, L3 kernel packet tests, L5 affected regression | Captures/counters, endpoint observations and exact set/state proof |
| Selective DIRECT/VPN, mandatory VPN, empty pool, unavailable/incompatible endpoints | Explicit/protected DIRECT remains valid; mandatory VPN rejects safely without implicit DIRECT; unknown capability/evidence is not remote DOWN | L1/L2; L3 native packet; L5 shared contracts; L7 authorized recovery only | Desired/generated/applied/native parity; forbidden-egress observations; no false success |
| IPv4/IPv6 within supported functionality | Supported modes route/isolate correctly; unsupported combinations explicitly rejected/documented | L2/L3 qualified network environment | Native routes/packet proof, supported-platform matrix; no unsupported IPv6 claim |
| RU/EN, locale change, missing translations | Correct locale state and documented fallback; no raw secrets or false operation success | L1/L2 locale registry; L3 real browser; L4 smoke | Browser UI→API→state and error assertions, actual locale/fallback evidence |
| Restart/update/recovery persistence; malformed/incomplete configuration | User intent preserved; failed apply retains last-good or truthful failed/unconfirmed state | L2/L3 isolated data, L5 fences/migrations; L7 authorized crash gate | Migration/recovery receipts, revision/incarnation/CAS, rollback/idempotency proof |
| Reboot persistence | Supported host boot restores verified intent/runtime without unsafe access loss | L7 separately qualified Debian host; never production fault injection | Console/recovery plan, boot/systemd/routes/native proof; NOT covered by Compose |
| Secrets and component isolation | Test-only canary secrets absent from API/UI/logs/traceback/exports/artifacts; components lack unauthorized access | L0 static, L1/L2 negative contracts, L3 isolated API/browser; L7 only if qualified | Bounded redacted canary scans and access-denial evidence; never production credentials |

Entry: supported config inventory, deterministic owned fixtures, isolation admission and reviewed expected outcomes. PASS requires all mandatory selected cases to execute with truthful failures/skips, configuration persistence and no secret/routing violations; archive source/version/plan, JUnit, native/packet/browser receipts and cleanup. Missing host environment remains an explicit release gap, not fabricated PASS.

## Database Architecture & Integrity Audit and evidence-based fixes

P1, PLANNED after Stage5. Inventory tables/relations, canonical versus derived/cache data, constraints/indexes/scans, orphan/stale rows, transactions/WAL/fsync/CAS, growth/write contention and supported migrations/rollback. Entry: accepted persistence/ownership contracts and representative isolated data. Exit: measured findings, integrity/migration/compatibility evidence and prioritized proven fixes. Keep Core/runtime/network/selector/recovery/Health/provider ownership out of database triggers. Do not implement future traffic analytics schema in advance.

## Stage 6 / 6A — cleanup and measured execution

The [single legacy candidate register](audits/operational_roadmap_legacy_2026-10-16/LEGACY_REGISTER.md) owns SAFE TO REMOVE / NEEDS MIGRATION / KEEP / UNKNOWN decisions. Classification alone never authorizes deletion. Operational may propose narrow measured early packages; schema/config contracts remain Stage5/Database Audit, module authority remains Stage8→7, physical cleanup Stage6. Optional adapters are not obsolete because absent from one installation. Each removal needs reachability/history/upgrade/recovery review and applicable regression/native evidence.


P2, PLANNED after Stage5 and evidence-based DB fixes. Stage6 removes only proven obsolete/unsafe compatibility and data handling; verify supported consumers first. Stage6A consumes the single Operational/Stage4 resource-efficiency backlog, rather than inventing another performance program. Exit requires affected correctness/security/native evidence and measured benefit; no speculative cleanup or optimization.

### Server-Wide Docker Compose Migration & Service Orchestration — PLANNED

P1 planning within Stage6A; physical migration gated by stable Stage8/Stage7/post-extraction interfaces and qualified Stage9 recovery. Scope is server-wide, not only FWRouter: establish ownership/dependencies for eligible services, independent Compose projects versus shared orchestration, networks/volumes/secrets, startup/readiness/restart/update ordering, observability and per-service rollback. Do not assume one giant Compose stack or migrate host-critical networking/Rescue without its own approved design.

FWRouter must retain **independent, authorized Docker API access**, without requiring a managed Xray/Mihomo/other replaceable module to be running or creating a circular lifecycle dependency. Design least-privilege socket/proxy/remote API authorization, scope mutations to explicitly owned resources and preserve access for independent management/recovery. Do not expose an unauthenticated Docker API or infer that a read-only socket mount provides read-only Docker authority. Preserve unrelated services and multiple external integrations.

Entry: read-only service/config/storage inventory, ownership/security review, stable application/adapter contracts, protected administrative access and proven restore points. Execution requires separately approved per-service migration windows; no blanket compose down/prune/reboot. Exit: reproducible manifests with immutable inputs, independent API/discovery/lifecycle tests (including absent runtime modules), same data/config/identity semantics, no cross-project mutation, retained DNS/Rescue/access and measured resource overhead/downtime; negative/recovery cases run only in qualified isolation. Final supported-host qualification belongs to Stage9, live approval to Stage10.

### Server-Wide Cleanup & Storage Audit — PLANNED

Scope: the entire Debian12 server named `minis` in this request (earlier evidence calls it `minisk`; verify identity before any future operation), not only FWRouter. Priority: pre-release maintenance after component stabilization, final action near release; read-only inventory may accompany Stage6. Inspect disk allocation, backups/snapshots, duplicate configs, Docker images/containers/volumes/build cache, systemd journal, app/debug logs, temp files/dumps, dev/test artifacts, staging/deployment copies, generated files and diagnostic retention.

Classify each object **keep / archive / delete after confirmation** with owner, purpose, dependencies, restore method and retention requirement. Protect production DB/active volumes/current backups, Rescue, certificates/keys/credentials, Home Assistant/Vaultwarden, historical audit reports/raw investigation evidence and current CI/release artifacts. No blanket `docker system prune -a --volumes` or deletion without separate reviewed authorization.

Entry: stable release candidate and storage/backup ownership map. Acceptance: approved deletion candidates, independent rollback/restore, measured space before/after, service/data integrity proof and justified log/backup retention. Evidence: inventory/classification, approvals, backup/restore receipt and bounded before/after measurements. No cleanup is authorized by this plan.

## Stage 8 → Stage 7 → Post-Extraction Audit

P1, PLANNED in this strict order after config/DB foundations. FWRouter Core is independently useful; Xray and Mihomo are **optional, replaceable implementations**, not mandatory platform services. Architecture must support alternative adapters, multiple module instances where capabilities permit, and multiple independent External Connections/API clients, including same-provider instances. Preserve stable per-instance/connection identity, credentials/config/state/cache/event/metrics scoping, lifecycle ownership and explicit exclusive-writer arbitration for shared global targets. Multiple connections never imply multiple writers of Core global state.

Stage8 entry: stable persistence, ownership inventory and existing adapter/registry evidence. Define versioned API/events/config/lifecycle/Health/capability and absence/unsupported behavior. Exit: explicit contracts and parameterized absent/disabled/alternative/same-provider/multi-instance/target-conflict tests with independent failure and no cross-instance leakage. Existing unit coverage is partial evidence, not full application acceptance of every combination.

Stage7 entry: Stage8 accepted; physically extract through those contracts without provider/runtime-specific Core dependencies or a parallel state/writer system. Exit: equivalent public behavior, migration compatibility, native proof and bounded resource/cleanup evidence. Post-Extraction Audit entry: physical extraction complete; exit: independent functional/architecture verification of boundaries, persistence/migrations, CAS/native/fail-closed, failure ownership and multiple-connection isolation. Do not start the reference study before both extraction and this audit are complete.

### Reference Architecture Audit — PLANNED study strictly after post-extraction acceptance

P2, existing post-extraction follow-up, not a new stage. Compare **3x-ui and analogous systems** only after Stage7 and Post-Extraction Audit are completed. Study traffic accounting/statistics, subscriptions, credential continuity/security/rotation, performance/resource cost, APIs and monitoring. Earlier external-UI research is background, not completion of this new qualified comparison.

Entry: accepted extracted architecture, supported feature inventory and comparison questions. Exit: versioned references, evidence-backed tradeoffs, applicability/licensing/security notes and adopt/reject/defer recommendations mapped to existing Core/Modules contracts and measured costs. No integration of3x-ui, hard dependency on any reference service, code transplant or implementation authorization is implied. Accepted Core ownership and independent adapters remain authoritative.

### External Integration Framework & SDK — PLANNED, late

Entry: DB/Core/Modules contracts and physical extraction/post-extraction audit stabilized. Define versioned integration API/events/config/lifecycle/authorization, ownership and supported SDK contracts without duplicating Core authority. Acceptance: compatibility/negative/isolation contracts, documented example against real supported APIs and evidence-backed versioning. Public SDK documentation belongs to Production Documentation after implementation. This remains separate from late Metrics Ingestion/Traffic Accounting and future-version UI/analytics/schema work.

## Hardening — Network/DNS Reliability & Security

Entry: configuration/persistence and relevant Core/Modules interfaces stabilized, before final production release. Existing Stage9 validates failure behavior. Neither DNS optimization nor expanded security matrix is an additional current PhaseE blocker without a demonstrated dependency.

### DNS Performance & Reliability — PLANNED

Priority: measured network reliability before release. Recorded infrastructure includes dnsmasq DHCP/DNS/local records/domain rules, FWRouter-generated dnsmasq config, installed BIND9, Mihomo/Xray and nftables routing. These are planning inputs, not a new live inspection; reverify actual configuration/ownership on entry.

Audit full DNS request chain and generated-config ownership; cache behavior, cold/warm p50/p95 latency, hits/misses/upstream traffic, CPU/RAM/disk I/O/background requests, upstream/VPN failures, DNS capture/nft sets and DIRECT/VPN/fail-closed priorities. Compare dnsmasq-only, dnsmasq+BIND9 and justified alternatives; assess cache size, TTL, prefetch, serve-stale/fallback and loop/leak risks. Existing BIND9 is not mandatory: retain/change role/remove only based on measured evidence and reviewed dependencies. Complexity is not performance, and reduced DNS latency alone does not prove faster websites.

Acceptance: DHCP/DNS correctness, preserved routing/fail-closed, no new leaks/loops, comparable before/after performance/resources, demonstrated benefit or confirmed risk removal, justified complexity and ready rollback. Evidence: topology/ownership, bounded query/packet/counter measurements, failure scope and restore plan. No changes now.

### Rescue isolation — classification correction accepted; residual safety audit PLANNED

P1 safety follow-up. The previous unconfirmed-LAN-classification wording is superseded: PR6/PR10 source and rollout evidence confirmed the classification fix and later live LAN/DNS/DHCP generation exclusion. External Rescue is PASS by user-confirmed manual verification; do not invent a fresh handshake. Legacy two Rescue DNS NAT rules remain unchanged, with zero counters in the recorded scope; neither exposure/leak nor complete isolation is established by zero counters alone.

Entry: read-only ownership/classification/DHCP/DNS/firewall audit and safe dependency/independent access review. Exit: actual boundary evidence, scoped isolated tests and a separately authorized reversible action for any proven residual defect. No cleanup or change to strongSwan/IKEv2/SSH/Tailscale/Rescue is authorized by this roadmap. Preserve earlier security findings and handoffs.

## Metrics Ingestion Optimization and Final UI/UX — PLANNED, late

P2, after extraction/post-extraction and DB/Core/Modules stabilization. FWRouter exports domain evidence and does not duplicate the separate server-wide host/container observability collector. Measure ingestion/accounting correctness, CPU/RAM/SSD writes, backpressure, retention and failure behavior before optimization. Do not prepare a future analytics schema, traffic DB or top-domain graphs now. Final UI/UX follows stable domain/API contracts and supplies the real screens for public documentation; supported language/fallback and security behavior need evidence.

## Production Documentation & Open-Source Presentation — PLANNED

Entry: mature UI/API/architecture and supported install/release contracts; before release. Keep `README.md` English and `README.ru.md` Russian with easy switching, concise purpose/features/architecture, verified screenshots and useful badges/diagrams/demos only. Put full navigable user/engineering documentation in `docs/`; keep internal handoffs/agent instructions/architecture working notes separate.

Deliver Installation/Quick Start, Initial Configuration, User Guide, Network/VPN and DNS guides, API Reference, Developer/Contribution guides, Security Policy, Troubleshooting, Backup/Update/Recovery/Rollback, supported platforms/limitations and full Test/CI technical documentation. Add implemented External Integration Framework/SDK docs when available; no unsupported feature claims.

Acceptance: both READMEs usable, coherent navigation, actual UI images, installation/update/recovery instructions verified in supported isolated environments, links/examples working and content matching application behavior. Evidence: reviewed docs/source versions, screenshots and instruction-validation receipts; no production operation merely to validate docs.

Public documentation must not become an exported directory of internal Codex sessions, private server notes, backups or raw investigation evidence. Audit public/internal placement and links deliberately; retain required history privately or as sanitized engineering records. This is a planned documentation restructuring, not deletion or a cosmetic overhaul in this reconciliation.

## Stage 9 — Recovery/Staging and Debian Host Acceptance

Preserve separately authorized L7 failure/recovery/crash gates and Configuration Matrix validation. Distinguish **Application Acceptance**, **Kernel Dataplane Acceptance** in isolated Linux namespaces and **Debian Host Acceptance**. Docker Compose does not prove host systemd, boot/reboot, physical interfaces, production-kernel compatibility or system-failure recovery.

Host gate: dedicated safe environment with independent console/access/rollback; inspect systemd lifecycle, boot/reboot restore, real interfaces, host routes/nftables/kernel compatibility and recovery. Destructive injection requires disposable qualified host/VM and authorization. If unavailable, record exact NOT RUN scenarios, impact and release disposition; never substitute mock/Compose PASS. Required evidence: environment qualification, boot logs, native route/state/packet proof and cleanup/recovery receipts.

Stage9 also reviews host activation/API-dependent timers, readiness-guard preservation, interruption/partial-apply rollback, code/data/native compatibility and the SQLite projection-commit/postimage-fsync crash window. Entry requires a genuinely qualified environment and independent console/restore access; a host label or Docker container is not PID1/systemd qualification. Never restore the unsafe generator with implicit DIRECT fallback. Evidence-limited release decisions must be explicit; no host/deployment qualification is fabricated by documentation.

## Stage 10 — Release / Production Live Acceptance

Entry: reviewed RC, applicable CI and separately authorized L6/L7/manual/host release gates; no automatic deploy. Prepare a safe production baseline, independent tested rollback and confirmed Rescue VPN/SSH/admin access. User approves exact deployment separately. Use the existing documented installer; deployment and needed managed restarts are explicit operations, not a consequence of PR approval/merge.

After deployment require Core desired/applied/native parity; Xray/Mihomo and bindings; DIRECT/VPN/DNS/DHCP; API/UI/local services; routes/resources/errors and bounded stability/no-oscillation observation. Capture exact deployed source/schema/config hashes, health/readiness/native/packet evidence, provider counters and access checks; rollback if critical invariants fail. Protect newer intent/data; no blind old-DB restore or provider mutation. No production destructive failure injection.

**CI Acceptance ≠ Production Live Acceptance.** Release acceptance uses safe isolated fault tests; live acceptance verifies the installed server after separately approved deployment. PASS requires healthy verified convergence, unchanged protected access and explicit treatment of residual host/manual gates. CI PASS, PR approval and merge never grant deployment authority; CD remains DEFERRED.

Before release, complete approved server-wide cleanup only against owner-classified safe candidates with protected backups/evidence and per-service restore; do not turn its Stage6 inventory into deletion authority. Keep three distinct receipts: **Synthetic Functional Acceptance**, **Production State Acceptance**, and **Controlled External E2E Acceptance**. Real credential/state inspection is authorized read-only evidence, not a synthetic fixture. External positive traffic requires an explicitly selected authorized available upstream; an intentionally unavailable test asset is a negative case only after purpose is proven. Do not waive a required positive E2E or call unavailable upstreams product bugs by default. The current recovery acceptance remains BLOCKED as recorded above.

## Preserved evidence gaps and deferred work

- P1/Stage8→7: confirm every External Connections registry/Rules typed-audit and multiple-active-instance application path. Existing lifecycle/adapter unit contracts and named native profiles are not full optional-module qualification.
- P1/Stage9: direct manual removal of the active VPN-auto member followed by fixed-target transition; SSH15-second timer clean-address readiness and Gateway/API lifecycle coupling; projection-commit/postimage-fsync crash consistency. Re-audit current implementation before proposing changes.
- P2/Stage6A or Final UI: reproduce Settings inventory/locale race before calling it a defect; measure LAN activity TTL without extending it to Xray/external/service clients.
- P2/Stage8: evidence-based Tailscale peer admission/inventory/per-subject policy; offline peers alone are not defects.
- P1/Stage5/security: token/client-token semantics and backup confidentiality/access/retention/restore. Current disclosure disposition is P0 above, not postponed by future planning.
- Historical October2 exception, Xray86→85 count change, unattributed provider counters and prior WAN reset causes remain UNKNOWN unless exact causal evidence is obtained. They are not new active product bugs or approved baseline exceptions. Original reports remain intact.
- The former changed-profile recovery/78-restoration tasks are complete in the recorded PR10 rollout scope. Separate positive client E2E and partial source availability remain open; neither is erased nor substituted by old refresh receipts.
- Future-version-only branch: removal of User mode/Admin-only controls, top-domain/traffic analytics/history graphs, expanded Xray accounting and new traffic schemas remain DEFERRED until the current version is released. No architectural prework now.

Each open item requires current-code reproduction/contract evidence, an owner and an approved bounded scope at entry; no inferred historical task starts automatically. [Decision register](PROJECT_MAP/DECISIONS/ROADMAP_DECISION_REGISTER_2026-10-15.md) retains priority/dependency/exit detail; [reconciliation report](audits/roadmap_decision_reconciliation_2026-10-15/REPORT.md) records what was already present, added or corrected.

## Reconciliation changelog

2026-10-16: synchronized canonical/Git active plan; archived exact divergent inputs; accepted bounded deployedPackage1, distinguished currentPackage2/3 from6004400; linked single Stage6-owned legacy register and explicit remaining closure gates. Runtime/config/security unchanged.
