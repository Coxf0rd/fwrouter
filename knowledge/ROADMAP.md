# FWRouter — active roadmap

## Current source and roadmap state — 2026-10-20

PR24–PR27 are merged in main at `94724aaa221aac90af08c3affef7a59acae76de1`, a single-parent squash of PR27 head `17b1ecfd1cedd31047406f9b866e04e5ad5270ae`. Main workflow [39438911410](https://github.com/Coxf0rd/fwrouter/actions/runs/39438911410) completed SUCCESS: mandatory suite required, exact source/plan/profile bindings, three hosted receipts and cleanup, aggregate eligible, 2047 PASS/0 FAIL/0 SKIP. This source/CI result is not production or full host acceptance.

The PR24–27 handoffs record qualified last-good recovery, durable Core intent/apply fencing, selective-auto transition and migration qualification, then fixed-mode lifecycle and strict admission. See [PR #24](https://github.com/Coxf0rd/fwrouter/pull/24), [PR #25](https://github.com/Coxf0rd/fwrouter/pull/25), [PR #26](https://github.com/Coxf0rd/fwrouter/pull/26), and [PR #27](https://github.com/Coxf0rd/fwrouter/pull/27). The server-only controlled deployment stop report (`knowledge/audits/controlled_deployment_94724aa_2026-10-20/FINAL_HANDOFF.md`, not published in Git) remains the boundary for production facts: current installed state and preflight were not read there, so production eligibility is not inferred.

**Stage 5 is READY but NOT STARTED.** Its server configuration and persistence contract work awaits a separate authorized production review and explicit user decision. This readiness authorizes no production read/write, deployment, admission, restart, migration or release acceptance. The interrupted fixed-mode checkpoint without a qualified preimage remains **BLOCKED**; destructive fixed-mode L7 recovery is NOT RUN. Full Debian host/reboot/recovery, external-client E2E, long-soak and Production Live Acceptance remain separate open gates. The P0 security and positive external-E2E residuals remain tracked in the [decision register](PROJECT_MAP/DECISIONS/ROADMAP_DECISION_REGISTER_2026-10-15.md).

Exact superseded active checkpoints are preserved in [the original mirror archive](history/roadmap_reconciliation_2026-10-20/MIRROR_BEFORE.md) and [canonical archive](history/roadmap_reconciliation_2026-10-20/CANONICAL_BEFORE.md). The reconciliation evidence is summarized in [the dated report](audits/roadmap_reconciliation_2026-10-20/REPORT.md). The canonical roadmap is `/решения/roadmap/fwrouter/ROADMAP.md`; this file is its Git mirror. Earlier evidence and its scope are not rewritten.

## Approved target architecture and active sequence

The approved target is a native Debian/systemd Core that independently owns durable intent, routing, apply, CAS/fencing and recovery. Docker, UI, Xray and Mihomo are not required Core dependencies. Optional specialized runtimes integrate through versioned adapter contracts. This is a future architecture decision, not a claim that the target has been implemented. Preserve the existing ownership, native proof, fail-closed, last-good and recovery contracts as each stage defines its interfaces.

1. **Stage 5 — Server Configuration and Persistence Contracts (READY, NOT STARTED).** Define supported server parameters and durable configuration through a headless API and API-first normal CLI. Keep the strict offline maintenance CLI separate. Then perform a separate Database Architecture & Integrity Audit and implement only evidence-based DB changes. Preserve the existing configuration/security matrix and expected outcomes.
2. **Stage 6 — Internal Restructuring.** Refactor large internal areas in bounded packages while preserving behavior and public APIs. At this source snapshot, `xray_subscription_service.py` is 7,647 lines, `services/selector.py` 2,415, `settings.js` 4,400, and `admin.js` 1,772. This stage is internal restructuring, not physical extraction.
3. **Stage 6A — Measured Performance and Server-Wide Compose Audit.** Measure performance before optimization. Audit Compose and service ownership across multiple functional projects, with host-critical services independently operable. Defer mass migration until after Stages 8/7 and host qualification. Server-wide migration is not a blocker for FWRouter Stage 7.
4. **Stage 8 — Core/Adapter Contracts.** Define small versioned contracts while specialized internals remain independent. Specialized formats, protocols, provider models, runtime configuration and lifecycle stay distinct; do not create a global subscription model or universal parser. Direct transformations are acceptable only when unsupported input fields are not silently discarded.
5. **Stage 8A — Adapter Registry and SDK (mandatory before Stage 7).** Specify the registry, SDK, manifests, capabilities, compatibility, errors, security, examples and contract tests. Third-party code must not run inside privileged Core or receive arbitrary SQLite access. Give each adapter least authority and explicit ownership.
6. **Stage 7 — Physical Extraction.** Extract Xray, Mihomo and UI only after Stage 8 and 8A contracts are accepted, then run a Post-Extraction Audit. UI control is a separate Docker component and remains a client; it has no routing authority and is not a runtime adapter.
7. **Late telemetry.** Go/Docker telemetry and traffic accounting follow Core/DB stabilization and extraction. Use bounded collectors, buffering, deduplication, backpressure, retention and redaction with least privilege. Do not migrate Core to Go or grant telemetry routing authority.

Do not expose an external API before a reviewed security design exists. Preserve Stage 9 host/recovery and Stage 10 release gates. Source or CI success does not prove production acceptance. The [decision register](PROJECT_MAP/DECISIONS/ROADMAP_DECISION_REGISTER_2026-10-15.md) owns detailed dependencies, priorities, security findings and unresolved gates.

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

## Operational Performance Fixes — accepted source scope; residual evidence tracked separately

The former PR22 closure block is superseded by later exact-source PR24–27 qualification and merge evidence above. Operational source work is accepted in its demonstrated bounded scope; this does not close production, full Debian host, external E2E, long-soak or release gates. Preserve historical package measurements and dispositions in the dated audit handoffs. Do not reopen completed packages or start optimization from this roadmap alone. D-12 remains UNKNOWN as to the cause of startup variability; preserve its historical limits/evidence and do not claim a performance improvement from unmatched observations.

### Timeouts & Thresholds Audit — PARTIALLY EXPLAINED; startup policy revised 2026-10-21

P1, owned by the same Operational re-audit, coordinated with Stage5 lifecycle and Stage9 host acceptance. The user explicitly approved a60s qualified API-readiness deadline with45s warning threshold on2026-10-21; this is an operational policy revision, not measured acceleration or a waiver of startup/recovery failures. Earlier45s outcomes remain historical evidence. Other timeout changes are not authorized by this decision. Trace each value to introduction/history, intended failure outcome, event boundaries, environment/workload and test evidence. Separate safety/correctness gates, operational deadlines and advisory performance targets; a time budget does not replace CAS, fencing or native proof.

| Historical value | Confirmed current meaning / remaining audit |
|---|---|
|40 seconds|Advisory hosted startup engineering target; not a hard PASS assertion. PR11 startup41.4643s did not meet this target |
|45 seconds|Historical hosted hard ceiling; now the slow-startup warning threshold. Healthy qualified readiness at or below45s passes normally; above45s and at or below60s passes with `SLOW_STARTUP` and measured elapsed/resource evidence |
|60 seconds|User-approved qualified API startup/readiness hard deadline. Healthy readiness must actually complete within60s; unready after60s is TIMEOUT/FAIL with retained diagnostics. Safety/integrity/recovery failure remains FAIL regardless of elapsed time; this is not a universal Debian live-start SLA |
|120 seconds|Source systemd `TimeoutStartSec` operational API start budget; distinguish unit/readiness scripts and historical installed versions |
|20 seconds|Source systemd `TimeoutStopSec` operational stop budget; not a benchmark target or permission to cancel durable writers |

Sources: `tests/application_acceptance/test_runtime_scale.py`, its conftest and `host/systemd/fwrouter-api.service`; these are source facts, not a new inspection of installed unit limits. Exit: reviewed threshold register with provenance, measured p50/p95/tail/headroom, environment sensitivity, timeout/error/cleanup behavior and change justification. Preserve truthful unready/failed state and bounded shutdown; never increase a timeout solely to hide a regression. Historically, Package1 hosted startup40.1817s passed the then45s hard ceiling but misses40s advisory by0.1817s; live journal startup26.104s is a different workload/environment. Historical PR11 values remain in evidence. No new universal SLA is invented.

## Stage 5 — Server Configuration and Persistence Contracts — READY, NOT STARTED

**Entry:** after the separate authorized production review and explicit user decision; inventory supported server parameters, ownership and current intent/generated/native representations first. **Work:** define parameters for interfaces, IP/subnets, gateways, ports, DNS/DHCP, paths, domains, runtime parameters, module configuration, Docker networks/mounts/volumes, secrets and permissions without hard-coded deployment assumptions; specify validation, headless API, API-first normal CLI, separate strict offline maintenance CLI, installer/defaults, update/migration, confidentiality, backup/restore and rollback. Version configuration and migrations; keep user intent, generated configuration, applied/verified state and last-good distinct. Update and restart contracts preserve state. Generated output is not user intent. **Exit:** reviewed parameter schema and security/configuration matrix, API/CLI examples marked planned until implemented, idempotent persistence/migration contracts, and a separate Database Architecture & Integrity Audit plan. Stage5 is READY but NOT STARTED; this roadmap authorizes no production operation.

### Configuration Matrix & Security Acceptance — retained Stage 5 contract

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

## Stage 6 / 6A — internal restructuring and measured execution

The [single legacy candidate register](audits/operational_roadmap_legacy_2026-10-16/LEGACY_REGISTER.md) owns SAFE TO REMOVE / NEEDS MIGRATION / KEEP / UNKNOWN decisions. Classification alone never authorizes deletion. Operational may propose narrow measured early packages; schema/config contracts remain Stage5/Database Audit, module authority remains Stage8→7, physical cleanup Stage6. Optional adapters are not obsolete because absent from one installation. Each removal needs reachability/history/upgrade/recovery review and applicable regression/native evidence.


**Stage6 entry:** Stage5 contracts accepted; trace current call paths, ownership, duplicated work, circular dependencies, compatibility shims, dynamic imports, tests and docs before each bounded restructure. Audit package/directory placement, naming, mixed responsibilities, tests and documentation structure. Current large audit targets include `services/xray_subscription_service.py`, `services/selector.py` and UI JavaScript; line count alone never justifies abstraction or file fragmentation. **Stage6 exit:** internal modules have explicit responsibilities, behavior and public APIs remain compatible, callers and extension paths are covered, migration/security/native contracts stay intact, and affected tests/docs pass. No physical extraction occurs here.

**Stage6A entry:** matched baseline and current server-wide project/service ownership inventory. Measure CPU/RAM, I/O and relevant workload costs before optimization. Audit Compose across multiple functional projects, dependencies, networks, volumes, secrets, startup, health, update and recovery. Host-critical Core, routing, access and rescue functions remain independently operable. **Stage6A exit:** measured changes have comparable evidence and bounded overhead; Compose audit has ownership, dependency and per-service recovery maps. Defer mass migration until after Stages8/7 and host qualification; it does not block FWRouter Stage7.

### Server-Wide Docker Compose Migration & Service Orchestration — PLANNED

P1 planning/audit within Stage6A; this is a multi-project, server-wide audit. Any mass migration is deferred until Stage8/7 interfaces and host qualification; it does not block FWRouter Stage7. Scope is server-wide, not only FWRouter: establish ownership/dependencies for eligible services, independent Compose projects versus shared orchestration, networks/volumes/secrets, startup/readiness/restart/update ordering, observability and per-service rollback. Do not assume one giant Compose stack or migrate host-critical networking/Rescue without its own approved design.

If a separately designed optional Docker-management adapter is retained, specify least-privilege authorization, explicitly owned resources, independent recovery and no circular dependency on Xray/Mihomo. Do not expose an unauthenticated Docker API or infer that a read-only socket mount provides read-only Docker authority. Core administration and routing must remain usable without Docker API access. Preserve unrelated services and multiple external integrations.

Entry: read-only service/config/storage inventory, ownership/security review, stable application/adapter contracts, protected administrative access and proven restore points. Execution requires separately approved per-service migration windows; no blanket compose down/prune/reboot. Exit: reproducible manifests with immutable inputs, independent API/discovery/lifecycle tests (including absent runtime modules), same data/config/identity semantics, no cross-project mutation, retained DNS/Rescue/access and measured resource overhead/downtime; negative/recovery cases run only in qualified isolation. Final supported-host qualification belongs to Stage9, live approval to Stage10.

### Server-Wide Cleanup & Storage Audit — PLANNED

Scope: the entire Debian12 server named `minis` in this request (earlier evidence calls it `minisk`; verify identity before any future operation), not only FWRouter. Priority: pre-release maintenance after component stabilization, final action near release; read-only inventory may accompany Stage6. Inspect disk allocation, backups/snapshots, duplicate configs, Docker images/containers/volumes/build cache, systemd journal, app/debug logs, temp files/dumps, dev/test artifacts, staging/deployment copies, generated files and diagnostic retention.

Classify each object **keep / archive / delete after confirmation** with owner, purpose, dependencies, restore method and retention requirement. Protect production DB/active volumes/current backups, Rescue, certificates/keys/credentials, Home Assistant/Vaultwarden, historical audit reports/raw investigation evidence and current CI/release artifacts. No blanket `docker system prune -a --volumes` or deletion without separate reviewed authorization.

Entry: stable release candidate and storage/backup ownership map. Acceptance: approved deletion candidates, independent rollback/restore, measured space before/after, service/data integrity proof and justified log/backup retention. Evidence: inventory/classification, approvals, backup/restore receipt and bounded before/after measurements. No cleanup is authorized by this plan.

## Stage 8 → Stage 8A → Stage 7 → Post-Extraction Audit

**Stage8 entry:** Stage5 persistence contracts and DB audit accepted; Stage6 ownership/call-path audit provides current boundaries. Define small versioned contracts while internals stay specialized and independent. Cover registration, capabilities, configuration, lifecycle, health/readiness, module instances, managed and external runtimes/connections, observations, native apply/proof, recovery, jobs and events; specify lifecycle, ownership, security, API/UI projection and compatibility boundaries. No universal subscription representation or parser is required: provider formats, protocols, provider models, runtime configuration and lifecycle remain distinct. A direct transformation must not silently discard unsupported fields. Preserve stable instance/connection identity and exclusive ownership for shared Core targets. **Exit:** contract versioning and capability/error semantics reviewed, with absent/disabled/incompatible and multi-instance isolation contracts and executable acceptance plan.

### Stage 8A — Adapter Registry and SDK — mandatory before Stage 7

Define a discoverable Adapter Registry layout and SDK; specify manifests, capability declarations, compatibility, errors, security, examples and contract tests. Capabilities must distinguish required from optional operations; no adapter must implement every possible operation. Third-party code must not execute inside privileged Core or receive arbitrary SQLite access. Include an API audit for authentication, roles, rate limits and UI projections. Mark planned CLI examples as planned, not implemented. Design adapter authority and data access explicitly, with specialized provider formats/protocols/runtime configuration kept distinct. Before extraction, perform a final placement audit confirming registry, SDK, manifests, examples and contract tests are discoverable and in their approved locations. **Entry:** Stage8 boundaries and actual adapter consumers reviewed. **Exit:** an independent test implementation plugs in through the accepted contracts without changing Core routing business logic or imitating Xray/Mihomo internals; developer documentation and negative security/isolation/compatibility tests pass. Stage8A is mandatory before Stage7.

### Specialized adapters and discoverable extension points — Stage 8A

The registry discovers/registers adapters, declares capabilities, selects compatible
implementations and transports boundary results/typed diagnostics. Runtime-specific
logic stays in its implementation. Parsing, validation, import, export, generation
and transformation are separate optional capabilities; adapters implement only
supported operations. Subscription encodings/formats (for example VLESS URI,
Clash YAML or sing-box JSON), transport/proxy protocols, provider APIs, runtime
configuration generators and runtime lifecycle are different responsibilities.
No mandatory global canonical subscription/configuration model is approved.
Local intermediate representations and direct transformations are permitted when
useful and lossless; unknown/incompatible/unsupported parameters must be rejected
or explicitly reported, never silently dropped. Each format gets independent
fixtures, validation and contract tests. A new format should normally add an
adapter and tests rather than change Core business logic.

Audit current `backend/fwrouter_api/adapters`, `services`, `subscription`, runtime
packages, imports and installer before choosing directory names. A possible
`adapters/{runtimes/{xray,mihomo,custom},subscriptions,protocols,collectors,external}`
and `sdk/{contracts,examples,tests}` layout illustrates discoverability only.
Distinguish public API contracts, adapter implementations, runtime internals,
server/user configuration, module packaging and tests; do not create this tree
or build a framework before real consumers justify it.

### Independent management clients — Stage 5 / Stage 8 / Stage 7 ownership

Stage5 owns headless configuration and safe local administration. Stage8 owns
public `/api/v2` completeness/versioning/authentication/authorization/client roles,
error/jobs/events, client compatibility, rate limiting and the separation of
UI-specific projections from Core contracts. API exposure outside the approved
local boundary requires a separate security design. UI, CLI and external API
clients use the same authorization, jobs, confirmations, revision/CAS, fresh proof
and auditing; no client gets direct SQLite/native access.

Develop the existing `fwrouter` CLI as an API-first normal management client.
`fwrouter status`, `modules list`, `routing show`, `clients list`, `jobs list`,
events and diagnostics are **planned examples**, not claims of implemented
commands. Keep existing API-independent offline migration/recovery maintenance
as a separate strictly controlled local mechanism. A dangerous action cannot
bypass safety by switching clients or entering offline mode.

Stage7 owns physical UI extraction after Stage8/8A: separate installation,
updates, lifecycle and full disablement, communicating through the public API.
The approved target is a UI Docker container in a suitable functional Compose
project; no frontend framework rewrite or membership in the runtime Compose is
required. UI loss/update must not affect routing. UI is a control client, not a
runtime adapter; telemetry and external integrations need their own appropriate
contracts rather than one forced interface.

### Stage 7 — Physical Extraction

**Entry:** Stage8 and 8A accepted, migration/rollback plans reviewed, and separately authorized implementation scope. Physically extract Xray, Mihomo and UI through accepted contracts while preserving public behavior and migration compatibility. UI control is a separate Docker client component with no routing authority and is not a runtime adapter. **Exit:** optional components can be absent or unavailable without disabling native Core intent/routing/apply/recovery; API behavior and supported migrations are compatible; native proof, fail-closed and rollback criteria pass. Run an independent Post-Extraction Audit of authority, persistence, CAS/native proof, recovery, security, instance isolation and failure behavior. Verify loss of Docker API/UI does not prevent headless API/CLI administration or Core recovery.

### Reference Architecture Audit — PLANNED study strictly after post-extraction acceptance

P2, existing post-extraction follow-up, not a new stage. Compare **3x-ui and analogous systems** only after Stage7 and Post-Extraction Audit are completed. Study traffic accounting/statistics, subscriptions, credential continuity/security/rotation, performance/resource cost, APIs and monitoring. Earlier external-UI research is background, not completion of this new qualified comparison.

Entry: accepted extracted architecture, supported feature inventory and comparison questions. Exit: versioned references, evidence-backed tradeoffs, applicability/licensing/security notes and adopt/reject/defer recommendations mapped to existing Core/Modules contracts and measured costs. No integration of3x-ui, hard dependency on any reference service, code transplant or implementation authorization is implied. Accepted Core ownership and independent adapters remain authoritative.

### External Integration Framework & SDK — delivered through mandatory Stage 8A

The required adapter SDK and registry contracts belong to Stage8A before extraction. Later integration-specific work may add versioned APIs/events/config/lifecycle/authorization without duplicating Core authority. Keep this separate from late telemetry and future-version UI/analytics/schema work.

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

**Entry:** persistence/DB, module contracts and physical extraction accepted. FWRouter Telemetry is planned as a separate Go module in Docker; Core is not migrating to Go. Collect Xray-client and LAN-client traffic, Mihomo data, logs, system/container events and other sources through specialized collectors; identify clients and handle counter reset/restart continuity. Logs, metrics and events remain distinct. Send observations to Core or explicitly authorized external destinations. Go/Docker collectors and traffic accounting follow DB/Core stabilization and physical extraction. Keep telemetry outside Core routing authority. Define event-versus-log semantics, counter reset/continuity and delivery guarantees. Design bounded collection, buffering, deduplication, backpressure, retention and redaction with least privilege, and measure ingestion correctness, CPU/RAM/SSD writes and failure behavior before optimization. Do not prepare a future analytics schema, traffic DB or top-domain graphs now. **Exit:** measured collection/delivery correctness, bounded allocations/queues/resources, explicit delivery semantics, client identity continuity, deduplication and failure/restart tests, least privilege and secret redaction. Telemetry cannot mutate routing intent, switch VPN, write Core SQLite or present observations as confirmed actions. Final UI/UX follows stable contracts; the control client remains a separate Docker component without authority.

## Production Documentation & Open-Source Presentation — PLANNED

Entry: mature UI/API/architecture and supported install/release contracts; before release. Keep `README.md` English and `README.ru.md` Russian with easy switching, concise purpose/features/architecture, verified screenshots and useful badges/diagrams/demos only. Put full navigable user/engineering documentation in `docs/`; keep internal handoffs/agent instructions/architecture working notes separate.

Deliver Installation/Quick Start, Initial Configuration, User Guide, Network/VPN and DNS guides, API Reference, Developer/Contribution guides, Security Policy, Troubleshooting, Backup/Update/Recovery/Rollback, supported platforms/limitations and full Test/CI technical documentation. Add implemented External Integration Framework/SDK docs when available; no unsupported feature claims.

Acceptance: both READMEs usable, coherent navigation, actual UI images, installation/update/recovery instructions verified in supported isolated environments, links/examples working and content matching application behavior. Evidence: reviewed docs/source versions, screenshots and instruction-validation receipts; no production operation merely to validate docs.

Public documentation must not become an exported directory of internal Codex sessions, private server notes, backups or raw investigation evidence. Audit public/internal placement and links deliberately; retain required history privately or as sanitized engineering records. This is a planned documentation restructuring, not deletion or a cosmetic overhaul in this reconciliation.

## Stage 9 — Recovery/Staging and Debian Host Acceptance

Preserve separately authorized L7 failure/recovery/crash gates and Configuration Matrix validation. Distinguish **Application Acceptance**, **Kernel Dataplane Acceptance** in isolated Linux namespaces and **Debian Host Acceptance**. Docker Compose does not prove host systemd, boot/reboot, physical interfaces, production-kernel compatibility or system-failure recovery.

Host gate: dedicated safe environment with independent console/access/rollback; inspect systemd lifecycle, boot/reboot restore, real interfaces, host routes/nftables/kernel compatibility and recovery. Destructive injection requires disposable qualified host/VM and authorization. If unavailable, record exact NOT RUN scenarios, impact and release disposition; never substitute mock/Compose PASS. Required evidence: environment qualification, boot logs, native route/state/packet proof and cleanup/recovery receipts.

Stage9 also verifies that native Core intent/routing/apply/recovery and headless API/CLI administration remain available when Docker management or UI control is absent; include these as explicit outage cases. Review host activation/API-dependent timers, readiness-guard preservation, interruption/partial-apply rollback, code/data/native compatibility and the SQLite projection-commit/postimage-fsync crash window. Entry requires a genuinely qualified environment and independent console/restore access; a host label or Docker container is not PID1/systemd qualification. Never restore the unsafe generator with implicit DIRECT fallback. Evidence-limited release decisions must be explicit; no host/deployment qualification is fabricated by documentation.

## Stage 10 — Release / Production Live Acceptance

Entry: reviewed RC, applicable CI and separately authorized L6/L7/manual/host release gates; no automatic deploy. Prepare a safe production baseline, independent tested rollback and confirmed Rescue VPN/SSH/admin access. User approves exact deployment separately. Use the existing documented installer; deployment and needed managed restarts are explicit operations, not a consequence of PR approval/merge.

After deployment require Core desired/applied/native parity; Xray/Mihomo and bindings; DIRECT/VPN/DNS/DHCP; API/UI/local services; routes/resources/errors and bounded stability/no-oscillation observation. Capture exact deployed source/schema/config hashes, health/readiness/native/packet evidence, provider counters and access checks; rollback if critical invariants fail. Protect newer intent/data; no blind old-DB restore or provider mutation. No production destructive failure injection.

**CI Acceptance ≠ Production Live Acceptance.** Release acceptance uses safe isolated fault tests; live acceptance verifies the installed server after separately approved deployment. PASS requires healthy verified convergence, unchanged protected access and explicit treatment of residual host/manual gates. CI PASS, PR approval and merge never grant deployment authority; CD remains DEFERRED.

Before release, complete approved server-wide cleanup only against owner-classified safe candidates with protected backups/evidence and per-service restore; do not turn its Stage6 inventory into deletion authority. Keep three distinct receipts: **Synthetic Functional Acceptance**, **Production State Acceptance**, and **Controlled External E2E Acceptance**. Real credential/state inspection is authorized read-only evidence, not a synthetic fixture. External positive traffic requires an explicitly selected authorized available upstream; an intentionally unavailable test asset is a negative case only after purpose is proven. Do not waive a required positive E2E or call unavailable upstreams product bugs by default. The interrupted fixed-mode checkpoint without a qualified preimage remains BLOCKED; this does not reopen the separately qualified PR24 last-good recovery. Stage9 host and Stage10 production/release gates remain distinct and open.

## Preserved evidence gaps and deferred work

- P1/Stage8→7: confirm every External Connections registry/Rules typed-audit and multiple-active-instance application path. Existing lifecycle/adapter unit contracts and named native profiles are not full optional-module qualification.
- P1/Stage9: direct manual removal of the active VPN-auto member followed by fixed-target transition; SSH15-second timer clean-address readiness and Gateway/API lifecycle coupling; projection-commit/postimage-fsync crash consistency. Re-audit current implementation before proposing changes.
- P2/Stage6A or Final UI: reproduce Settings inventory/locale race before calling it a defect; measure LAN activity TTL without extending it to Xray/external/service clients.
- P2/Stage8: evidence-based Tailscale peer admission/inventory/per-subject policy; offline peers alone are not defects.
- P1/Stage5/security: token/client-token semantics and backup confidentiality/access/retention/restore. Current disclosure disposition is P0 above, not postponed by future planning.
- Historical October2 exception, Xray86→85 count change, unattributed provider counters and prior WAN reset causes remain UNKNOWN unless exact causal evidence is obtained. They are not new active product bugs or approved baseline exceptions. Original reports remain intact.
- The former changed-profile recovery/78-restoration tasks are complete in the recorded PR10 rollout scope. Separate positive client E2E and partial source availability remain open; neither is erased nor substituted by old refresh receipts.
- Future-version-only branch: removal of User mode/Admin-only controls, top-domain/traffic analytics/history graphs, expanded Xray accounting and new traffic schemas remain DEFERRED until the current version is released. No architectural prework now.

Each open item requires current-code reproduction/contract evidence, an owner and an approved bounded scope at entry; no inferred historical task starts automatically. [Decision register](PROJECT_MAP/DECISIONS/ROADMAP_DECISION_REGISTER_2026-10-15.md) retains priority/dependency/exit detail; [October 15 reconciliation report](audits/roadmap_decision_reconciliation_2026-10-15/REPORT.md) records earlier decisions; [October 20 report](audits/roadmap_reconciliation_2026-10-20/REPORT.md) records this update.

## Reconciliation changelog

2026-10-16: synchronized canonical/Git active plan; archived exact divergent inputs; accepted bounded deployedPackage1, distinguished currentPackage2/3 from6004400; linked single Stage6-owned legacy register and explicit remaining closure gates. Runtime/config/security unchanged.

2026-10-20: superseded PR22 operational-closure/Stage5-blocked active checkpoint with PR24–27 merged source and exact main-CI evidence; recorded Stage5 READY/NOT STARTED and approved native Core / Stage8A-before-Stage7 sequence. Preserved original mirror and canonical snapshots, handoffs, configuration/security matrix, security findings and residual acceptance gates. Documentation only; no production state or runtime behavior changed.

Canonical source: `/решения/roadmap/fwrouter/ROADMAP.md`. Relative documentation links in this synchronized text resolve from the Git mirror `/srv/fwrouter/knowledge/ROADMAP.md`; canonical placement does not imply copies of private evidence beside it.
