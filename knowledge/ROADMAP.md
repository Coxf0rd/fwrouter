# FWRouter engineering roadmap mirror

## PR8 atomicity and load requalification — 2026-10-15

Run38646554002/sourceeb295ac SUCCESS: component atomicity checks, full native identity1 and startup/load2 PASS. Startup35.5317/35.8416s;48/48 successful concurrent reads, healthmax0.0099s/allAPImax1.825s; HWM938144/904396KiB, shutdown0.2138s. Exact82synthetic identities/native proof/cleanup preserved. Prior finalfull38642672427 had1525PASS/1FAIL duepre-checkpoint identity persistence; invocation-owned compensation nowqualified. Final fullrequiredPR rerun pending beforeReady; no production/liveacceptance. Previous near45srun remainsvarianceevidence. CI milestone CLOSED/CD DEFERRED.

## PR8 hosted load acceptance — 2026-10-15

Run38641631465/source43e8b7b SUCCESS: native identity case1 plus startup/load2 PASS.48/48 concurrentAPI reads successful; healthmax0.0071s, otherAPImax2.3345s, publicXray~0.184s, shutdown0.2639s; exact82 identities/native proof/cleanup retained. Startup44.5947/44.9644s meets45s but remains near threshold; advisory40s not met. HWM854656/997800KiB is API process lifetime, not aggregate/native. This confirms short synthetic load, not longsoak/liveproduction. Final full requiredPRCI pending, PR8 Draft; production held/Live Acceptance BLOCKED, CI milestone CLOSED/CD DEFERRED.

## PR8 load qualification / engineering threshold — 2026-10-15

Run38617926070 confirms native identity acceptance but scale stops at an added40s engineering assertion: actual readiness43.5133s meets45s hard budget. Per user clarification40s is advisory; retain45s and all actual load/health/memory/shutdown/native predicates. New scoped load repeats pending; no full requiredCI or production activation yet. Prior40s FAIL remains historical evidence. PR8 Draft, production held/Live Acceptance BLOCKED; CI milestone CLOSED/CD DEFERRED.

## PR8 startup bottleneck follow-up — 2026-10-15

Sourcecd4584f run38605119405 confirms complete isolated native durable identity/migration/refresh/restart/rollback acceptance. Startup readiness still FAIL at45s;46.3387s includes timeout handling and is not successful startup. Second scale attempt/final full CI NOT RUN. Measured live routing12.8421s and Xray recovery27.1367s dominate; timeout enters a duplicate final Mihomo reconcile after staged native publication. A guarded completed-publication receipt removes only duplicate work; legacy/no-proof paths retain native checks. Hosted improvement and40s engineering margin remain unconfirmed. PR8 Draft/BLOCKED, production API/dependents held without changes, CI milestone CLOSED/CD DEFERRED. See runtime stability REPORT.md; historical checkpoints remain below.

## PR8 durable continuity hosted checkpoint — 2026-10-14

Pushed f92f08a run38580161202 confirms subscriptionL1 13 PASS, exact migrationL3 8 PASS, adjacentXrayL3 54 PASS and MihomoL3 48 PASS. Real CLI intent-only mapping preserves78 preimages and8 affected bindings, but native fresh supersession remains FAIL at a dangling old Mihomo profile member; complete migration/publication and new startup measurements are not accepted. Bounded hashed graph diagnostics are being added before any speculative retention change. Source review also identified stale historical-mapping replay and unsafe preimage policy activation risks; current-identity CAS and pre-apply fail-closed/native rollback validation are under qualification. Startup44.8415s/45s margin remains unresolved. Full required CI is reserved for the final coherent checkpoint; PR8 Draft/BLOCKED, Production Live Acceptance BLOCKED, production units held with no mutations. CI milestone CLOSED/CD DEFERRED. Historical checkpoints remain below; current evidence: api_runtime_stability_2026-10-13/REPORT.md.

## PR8 durable continuity implementation — 2026-10-14

User approved persistent logical-node credential mapping, including all78 original production identities and internal VPN-auto continuity. Source/test drafts are being integrated in PR8; migration, refresh, restart, recovery and rollback hosted qualification are NOT RUN. The protected operator migration must precede ordinary generation; stale legacy checkpoint resume is forbidden and fresh owned supersession requires preserved evidence, atomic checkpoint replacement and all current fences/native proof. Conflicts/deletion/explicit rotation must be qualified. PR8 remains Draft, Production Live Acceptance BLOCKED, production API/dependents held with no mutation in this phase.

Startup44.8415s/45s remains an unresolved margin risk despite repeat28.1711s. Only a confirmed redundant subject lookup is being removed; no measured improvement or sufficient margin is claimed. Targeted qualification follows the coherent package; one full CI is reserved for final readiness. CI milestone stays CLOSED, CD DEFERRED, unrelated milestones remain paused. See api_runtime_stability_2026-10-13/REPORT.md; historical checkpoints below remain preserved.


## PR8 repeated hosted qualification / recovery decision gate — 2026-10-14

47d64e2 run38551736466 SUCCESS, unchanged runtime/test/workflow code relative to successfulca1cd8a run38548698209. Each:1451 routine,26+1 qualified native,26 ordinary,11 Provider,2 packet/scale PASS,0 FAIL/SKIP. Nonempty82 identities retained exactly/82 bindings/7 verified snapshots/actual Reality TCP VPN-source observations/24 HTTP200 reads. Startup28.1711s and44.8415s, HWM947256/908944KiB, max API1.5216/2.2285s, health0.0041/0.0118s, observed shutdown0.2136/0.2639s. Two scoped PASS; variance/near45s margin remains a risk, no host-systemd/soak claim.

Production4ffd686 API/dependents remain held and original78 recovery NOT RUN. Persistent credential continuity across changed canonical profile IDs requires an explicit durable mapping/compatibility decision; proposed scoped migration NOT IMPLEMENTED, frozen last-good versus qualified new-profile migration are distinct choices. No unsafe generator rollback, no fence bypass, no guessed alias. PR8 DRAFT/recovery decision gate, Live Acceptance BLOCKED. CI milestone CLOSED, CD DEFERRED; no new milestone. Current engineering report records historical FAIL and scoped PASS separately.

## PR8 first strict nonempty PASS — 2026-10-14

Actualca1cd8a PR run38548698209 SUCCESS:1451 routine, qualified26+1, ordinary26, Provider11 and packet/scale2 PASS; no FAIL/SKIP. Real nonempty startup44.8415s, API HWM908944KiB,24 concurrent HTTP200 responses (max2.2285s; health max0.0118s), observed graceful shutdown0.2639s.82 native identities exactly preserved,82 matching bindings,7 verified snapshots; actual Reality HEAD counted at the endpoint with VPN source. Strict deadline rejects late readiness. Startup margin0.1585s is narrow; repeat qualification pending. Remote availability of all synthetic servers is not inferred from this performance case; canonical fixture selector health is unmeasured, separate Provider cohort PASS.

Production remains installed4ffd686 with API/dependents held; original78 restoration and durable profile continuity NOT IMPLEMENTED/NOT RUN. Legacy checkpoint fences still reject direct resume; current source does not retrofit transition proof. Proposed scoped identity migration must cover repeat refresh, Core/CAS/native publication and rollback compatibility before implementation. PR8 DRAFT/BLOCKED and Production Live Acceptance BLOCKED; no deployment/activation/config/UUID writes in this diagnostic phase. CI milestone CLOSED/CD DEFERRED. Historical checkpoints below remain evidence, not current acceptance.

## PR8 current hosted diagnostic checkpoint — 2026-10-14

Source499d23f run38540649011: startup43.265s, HWM803828KiB, observed cleanup2.3185s, but scale FAIL at one newly generated internal vpn-auto identity. Early diagnostics establish the fixture omitted the virtual internal target that `_vpn_auto_servers_for_xray_subscription` always returns. Corrected fixture preloads82 identities (70 public,10 internal including virtual,2 fixed), requiring exact unchanged native/binding sets before/after; hosted verification pending. Previous92de4c9 run1451 routine PASS including full-JSON7→10-member identity regression, qualified26+1/ordinary26/Provider11/original packet1 PASS, but45.64s startup FAIL. Thus stability/Reality TCP/steady HTTP are not accepted.

Original production78 credentials remain in protected DB/preimage; seven published credentials absent from native after partial convergence. Durable profile identity continuity is proposed but NOT IMPLEMENTED; it must preserve Core/CAS/native proof and cover next refresh/rollback, rather than rewrite snapshots or alias by endpoint. Production API/dependents remain held, no config/identity/activation writes; PR8 DRAFT/BLOCKED and Live Acceptance BLOCKED. CI milestone stays CLOSED/CD DEFERRED. Historical checkpoints below are preserved; current report is api_runtime_stability_2026-10-13/REPORT.md.

## PR8 current diagnostic clarification — 2026-10-14

Production remains installed4ffd686 with API/dependent units held; this diagnostic phase performs no activation/reload/UUID or configuration mutation. Protected raw JSON evidence shows the provider profile gained3 VLESS members (7→10), changing its canonical full-profile hash/server ID before activation. Seven public and one internal derived credentials changed;70 retained. Original78 remain in DB/preimage, seven published credentials are absent from native, and actual client authentication impact is not observed. Stable credential recovery across that changed logical-profile key needs authoritative continuity mapping; host/name/primary-endpoint aliasing is not accepted.

Legacy xray_applied has stale selection/Mihomo fences and no persisted transition digest; direct resume is inadmissible. New source preserves/fences transition proof, removes repeated per-client native preflight and reuses only bounded immutable expected-config analysis/identical fenced candidate bytes. Native proof, both validators, CAS, fail-closed and protected DIRECT remain mandatory. The new planner/checkpoint/candidate cases execute in routine L3 (the earlier L1 wording below is superseded). Latest completed38489691937:1450 routine PASS; qualified26+1/ordinary26/Provider11/original packet1 PASS, nonempty startup scale1 FAIL at unchanged45s, HWM791620KiB. Actual d4ba217 run38491844089:1451 routine PASS, qualified26+1/ordinary26/Provider11/original packet1 PASS; nonempty startup36.7484s and observed graceful cleanup1.6666s, HWM809980KiB, but scale FAIL at82 versus81 applied bindings. Shared auxiliary upstream inbound is now isolated from the dedicated scale fixture; exact identity-set assertion retained/strengthened, hosted repeat pending. Reality TCP/steady health remain NOT RUN; no full scale acceptance is inferred. PR8 remains DRAFT/BLOCKED; Live Acceptance BLOCKED; external Rescue prior user-confirmed PASS. CI milestone remains CLOSED/CD DEFERRED; no next milestone. See runtime stability REPORT.md/LIVE_ACCEPTANCE.md.

## PR8 read-only recovery diagnosis — 2026-10-14

Production API/dependents remain held on installed4ffd686; no further runtime mutation. Identity forensics attributes7 subscription UUID replacements to one persisted inventory-server change plus1 internal vpn-auto identity; original78 are preserved in DB/checkpoint preimage but public/native publication is inconsistent. Revision/incarnation mismatch and8 invalid inventory bindings block blind recovery. Hosted38478166408 reproduces the nonempty-profile startup deadline in redundant per-client native preflight; source fix and exact L1 regressions are under qualification. Its1439 routine/26+1 qualified/26 ordinary/11 Provider PASS do not qualify the2 failed packet/scale scenarios or the newly registered component cases. Full Production Live Acceptance and PR8 merge readiness remain BLOCKED. External Rescue remains user-confirmed PASS; no unsafe generator rollback. See runtime stability REPORT.md for immutable checkpoints and recovery plan. CI milestone CLOSED/CD DEFERRED; no next milestone.


### Runtime stabilization qualification — 2026-10-14

PR8 remains BLOCKED after a user-authorized controlled feature-source backend/docs rollout. Hosted38472054455 fully PASS (1439 routine, qualified26+1, ordinary26, Provider11, packet/scale2); scale startup30.0161s/loadmax2.3812s/shutdown0.2137s/APIHWM892128KiB. Exact4ffd686 is installed, host guard preserved; live API failed120s readiness/20s stop, RSSpeak751428KiB. Native count78 but8 UUID replacements and generation checkpointxray_applied require investigation/safe recovery; no further activation or native changes after this critical uncertainty. API/dependent units held; observed native DIRECT/VPN/DNS and Rescue preserved. Hosted empty-profile scale does not qualify nonempty live subscription-profile convergence. Full Live Acceptance remains BLOCKED; unsafe implicit-DIRECT rollback prohibited. See api_runtime_stability report/LIVE_ACCEPTANCE. CI CLOSED/CDDEFERRED; no next milestone.

## 2026-10-13 — API scale stabilization after PR7 merge

PR7 is merged as main1b302e9; actual main38400414256 PASS (217 routine +26 qualified native child, mandatory native smoke and eligible source/plan aggregate). Production still runsa4a7a33 with API/dependent workers/timers held. Parser-only correction does not qualify full startup/memory/shutdown. Branch `fix/api-runtime-health-scale-stability` addresses duplicated readonly health, temporary rule allocations, scheduler/prewarm ownership and stale owned Rescue LAN markers; realistic hosted scale acceptance is pending. No deploy/restart/firewall change. Live Acceptance remains BLOCKED; next stages remain paused. See `knowledge/audits/api_runtime_stability_2026-10-13/REPORT.md`.


## 2026-10-13 — controlled production rollout; Live Acceptance BLOCKED

User authorized backend/docs deployment of exact maina4a7a33 and accepted incomplete host rollback qualification. Standard installer from verified Git export completed;434 files/guard verified, Xray78/78, native DIRECT/VPN TCP and observed VPN UDP/no-clear-fallback PASS. Generated Rescue LAN hostnames/DHCP DNS corrected. Production is updated, not UNCHANGED.

API/control plane and its managed dependent workers/timers are now temporarily STOPPED for resource safety (RSS grew to~5.8GiB, shutdown hit20s timeout); native Xray/Mihomo/DNS/Rescue remainactive and real DIRECT/VPN stillPASS. Exact prior unit states saved; restore after verified fix rollout/readiness. Live Acceptance remains **BLOCKED**: startup254s vs31s,16/31 health timeouts during15min, repeated40s traffic collector timeout and increased API RAM; two inherited native Rescue LAN ingress entries still await Core reconciliation. Only traffic collection timer temporarily held (enabled state unchanged); no host/Rescue/network-policy weakening or unsafe old rollback. Source parser correction is [PR7](https://github.com/Coxf0rd/fwrouter/pull/7), scoped217+26 PASS at38396231843; fix not merged/deployed. See `knowledge/audits/production_deployment_live_acceptance_2026-10-13/LIVE_ACCEPTANCE.md`. CI CLOSED, CD DEFERRED; no next milestone. Prior readiness statuses below remain historical and are superseded only within this scope.


## 2026-10-13 — pre-deployment safety follow-up

CI milestone is CLOSED (merged ef46147; actual main run 38363962787 PASS). External Rescue is PASS by user-confirmed manual external verification; the earlier inability to test it is superseded, without invented automated evidence. A separate `fix/predeploy-rescue-isolation-safe-rollback` safety branch addresses reserved Rescue LAN discovery and qualified recovery targets. Source and scoped hosted qualification passed (38381629814:1461/0/0); PR #6 required checks determine final merge readiness; full host rollback activation is NOT QUALIFIED; production rollout and live acceptance remain NOT RUN / BLOCKED. Archived 3d285c3/c1e00b5 is not a safe mandatory-VPN rollback target. Host/readiness guard remains outside deployment scope. See the safety report at `/srv/fwrouter/knowledge/audits/predeploy_rescue_safe_rollback_2026-10-13/REPORT.md`. No Operational Performance Fixes, Stage 5 or CD work is started.



## Documentation consolidation — 2026-10-13

Documentation-only baseline `5decb99`; no new test/runtime result. Former summary requirements now belong to CI/Git, Stage5 matrix, Stage6 storage, late SDK, hardening DNS/Rescue, Production Documentation and Stage9/10. [Exact pre-edit mirror](history/ROADMAP_PRE_2026-10-13_PRODUCTION_READINESS.md) preserves all earlier wording. Main-event verification follows authorized merge and is not a pre-merge prerequisite.

## Current acceptance pointer — 2026-10-13

**Phase D: ACCEPTED (hosted implementation/mandatory gates). Entire Test Architecture & CI Stabilization milestone: OPEN; Phase E PRE-MERGE ACCEPTED at PR5/run38347054113; READY FOR USER MERGE after latest protected HEAD checks pass. Post-merge main validation remains OPEN.** Source `a10dd9c`, [run38305978196](https://github.com/Coxf0rd/fwrouter/actions/runs/38305978196): affected1429 PASS/0 FAIL/0 SKIP; qualified26+1 and minimal application/native1 PASS; ordinary26/Provider-kernel11/packet1 PASS twice with exact source/plan/native/capture/cleanup receipts. Historical exact-seven7/7 PASS, no baseline allowlist. Targeted cache/prewarm/state300 nodes PASS five times (38305595288). See Phase D REPORT and `FULL_BUNDLE_CHECKPOINT_38305978196.json` for scope/resource limits.

L0–L4 primary proofs plus L5 affected-regression anchors; no L6/L7 execution. GHCR digest-pinned Debian12 base and fresh-source consumers confirmed; Compose is not Debian host/reboot acceptance. Actual PR/main event execution and protected required-check promotion remain for authorized final CI acceptance. No production changes/deploy or CD. Operational Performance Fixes remains PAUSED/BLOCKED in its preserved branch. Phase E is authorized and in progress; do not start another milestone automatically. Earlier checkpoint/status sections below preserve history and are superseded only within the exact scope above.


## Historical CI checkpoint ledger — reference only

All intermediate BLOCKED/OPEN/NOT RUN wording in this ledger records the dated run, not current status. The acceptance pointer and active execution order supersede it only within their proven scopes. Reports and raw evidence are preserved.

## Historical hosted checkpoint — 2026-10-13 (superseded by acceptance pointer)

Run [38291414020](https://github.com/Coxf0rd/fwrouter/actions/runs/38291414020), source `dfa583f`: ordinary26, Provider/kernel11 and packet1 all PASS twice with distinct owned receipts, no FAIL/SKIP and clean cleanup. Qualified Mihomo/child26, native Xray1 and minimal application/native1 PASS. The current affected unit leg is1419 PASS/2 FAIL/0 SKIP; all historical exact-seven are included and PASS. Overall run remains FAIL: selective IP-only candidate assertion and installer dependency dry-run root requirement remain under targeted attribution. Unit CLI correctly returned1, but the workflow wrapper omitted propagating that exit and unnecessarily ran the hosted bundle; strict final aggregation still rejected the failed unit row. No false aggregate acceptance is claimed. Reviewed bounded evidence: `FULL_BUNDLE_CHECKPOINT_38291414020.json`; raw evidence remains untracked. Next gate is narrow fixes/verification, then complete source-matched aggregation. Phase D BLOCKED; production/Phase E/CD/L6/L7 untouched. Earlier checkpoint notes are historical scoped evidence.

Latest residual unit acceptance38291091850 (37ae2b6):149 PASS/0 FAIL/0 SKIP; preflight57/108/41/13 PASS. Known shared fixture/runner clusters are closed only in unit scope. Next mandatory gate: qualified native + fixed-base affected L0-L5 + exact26 ordinary/11 Provider-kernel/1 packet bundle and green-only repeat. PhaseD BLOCKED pending complete current-source acceptance; no L6/L7/production/PhaseE/CD execution.

Latest narrow hosted run38270929907 (abc3b33):371 PASS/7 FAIL/0 SKIP across378 unit/component testcases; earlier362/15 checkpoint and57 gate/108 acceptance preflight retained as history. Confirmed fixture corrections preserve actual application transitions and strict guards; native forwarding acceptance is separate. Seven remaining apply/bypass fixture paths under targeted correction; full26+11+1 bundle and affected reproducibility remain OPEN. PhaseD BLOCKED; no production/PhaseE/CD/L7 changes.

Latest narrow hosted run38265018734 (2384fd7): fixed24 cohort353 distinct testcase PASS/24 FAIL/0 SKIP; reported354 includes one synthetic Node file-summary. Xray debounce21/21 PASS; gate54 PASS; acceptance106 PASS/1 FAIL/1 ERROR. Confirmed suite TMPDIR/Node accounting and incomplete receipt/native fixtures require targeted correction; other application assertions remain under attribution. No broad rerun or production changes; PhaseD BLOCKED. See PhaseD REPORT and UNIT_FIXTURE_CHECKPOINT_38265018734.json.

## Current broad-gate blockers — 2026-10-12

Run [38257165004](https://github.com/Coxf0rd/fwrouter/actions/runs/38257165004), `becc5ed`: qualified native gates PASS; routine1324 PASS/114 FAIL,3 missing runner adapters; functional33 PASS/5 FAIL (38 collected, no SKIP). Confirmed shared causes: unit host-discovery needs explicit isolated fixtures while subprocess guard remains strict; functional file-level selection flattens kernel11/packet1 into ordinary profile. Implement exact exhaustive ordinary26 + Provider/kernel11 + packet1 partition with native/source/JUnit proof and no capability broadening. Remaining individual result-contract failures under attribution; no blanket baseline allowlist. Historical exact-seven five current PASS/two Xray uncollected. GHCR kernel/packet acceptance remains confirmed in scoped38255994863. Fresh full gates/reproducibility remain OPEN; Phase D BLOCKED. Production/Phase E/CD/L7 untouched.

## Current Phase D delivery checkpoint — 2026-10-12

GHCR kernel/packet consumer [38255994863](https://github.com/Coxf0rd/fwrouter/actions/runs/38255994863), `dcf3e79`: expanded native packet **1 PASS / 0 FAIL / 0 SKIP**,60.431s, owned cleanup/host snapshots PASS. Same digest-pinned source-free dependency image plus fresh-source overlay now serves ordinary/kernel/packet profiles. Publication38254752823 and anonymous cold/warm consumer38255208001 PASS; new base2616513753B, cold38.856s/warm0.127s, packet overlay build7.957s versus previous29.001s (individual observations, no aggregate performance claim). Qualified source-manifest omission fixed in4a7b823; repeat [38256549625](https://github.com/Coxf0rd/fwrouter/actions/runs/38256549625) PASS: qualified Mihomo/child26/26, Xray native1/1, source-manifest regression1/1 and minimal application/native1/1, no SKIP, cleanup confirmed. Full functional/affected and reproducibility OPEN; Phase D BLOCKED. Production/Phase E/CD/L7 untouched; Debian host/reboot parity remains separate release acceptance. Detailed evidence in Phase D REPORT and GHCR_PACKET_CHECKPOINT_38255994863.json. Prior checkpoint notes below retain historical scope.

## Latest confirmed packet checkpoint — 2026-10-12

Run [38249879539](https://github.com/Coxf0rd/fwrouter/actions/runs/38249879539), `74bd48f`: expanded native packet scenario **1 PASS / 0 FAIL / 0 SKIP**, 59.189s; exact selective/fingerprint/fence regressions12/12, DNS freshness8/8 and actor/audit3/3 PASS. Selective DNS priorities, actual public VPN-auto switch to second VLESS endpoint (TCP/UDP/DNS), native/Core parity, exclusive Provider restoration, zero forbidden DIRECT deltas/extra provider calls and owned cleanup are confirmed. Host namespace/firewall/routes unchanged. Derived active selection no longer invalidates structural generation hash; independent revision/operation/incarnation fences remain. GHCR kernel migration, fresh qualified/full functional/affected and reproducibility gates OPEN; Phase D BLOCKED. Production/Phase E/CD/L7 NOT RUN. Prior checkpoints below retain historical scoped evidence.

## Active targeted checkpoint — 2026-10-12

Phase D BLOCKED. Run [38247909408](https://github.com/Coxf0rd/fwrouter/actions/runs/38247909408), `309c730`: 10 focused regressions PASS; expanded native packet scenario FAIL at final exclusive restoration. Selective DNS priority/restoration and actual public Auto switch to the second VLESS endpoint satisfy their packet/native/counter checks, with zero extra provider requests and forbidden DIRECT traffic. The whole scenario remains FAIL. Exact refusal: derived `active_auto_server_id` changes the structural input hash while revision33, operation, incarnation and active-config fences match. Source `84bce97` excludes only that field, which the config builder does not consume; independent selection-revision fencing and actual intent inputs remain. Targeted run [38249633437](https://github.com/Coxf0rd/fwrouter/actions/runs/38249633437) IN PROGRESS, not PASS. Ordinary GHCR fresh-source application/native and historical exact-seven are accepted only in the scope of run38210552876. Kernel GHCR migration, full functional/affected and reproducibility remain OPEN. No production/Phase E/CD/L7 action. Earlier checkpoints below retain historical scoped evidence.

## Latest bounded native attribution — 2026-10-12

Packet run `38211674568` remains 0 PASS / 1 FAIL / 0 SKIP (68.008 s). Live nft read succeeds and contains the degraded selective/default-DIRECT marker; the parser's outer normal-selective predicate prevents its degraded branch from running. Recognition correction and attribution of degraded generation are pending, without bypassing readiness or mandatory-VPN policy. Actual selective priority and target-switch packet assertions were not reached. Ordinary-profile GHCR application consumer `38210552876` is confirmed 1/1 native PASS plus historical exact-seven 7/7 PASS; kernel profiles retain their existing pinned build. Full functional/affected/reproducibility remain OPEN. Phase D BLOCKED; production, Phase E/CD/L7 untouched. See the current Phase D report; older dated entries remain historical evidence.

## Current targeted checkpoint — 2026-10-12 (native diagnostics)

Run [38208049662](https://github.com/Coxf0rd/fwrouter/actions/runs/38208049662), `4fb724b`: fixed Provider application/native/browser cohort **11 PASS / 0 FAIL / 0 SKIP**, suite 150.318 s; cleanup errors empty. Earlier two re-entry failures are resolved by validated receipt publication before the harness barrier. Full functional/affected/historical gates, additional packet target-switch/selective proof, GHCR application consumption and reproducibility remain OPEN. Phase D BLOCKED; production unchanged.

Run [38207720641](https://github.com/Coxf0rd/fwrouter/actions/runs/38207720641), `35a7353`, passes both real native re-entry concurrency cases: **2 PASS / 0 FAIL / 0 SKIP**, suite 34.995 s. Current owned member probes are HTTP 200, 2 ms each; exclusive-intent and real Mihomo-incarnation fencing assertions execute and pass. The prior apparent timeout blocker was stale test-harness receipt publication, not a confirmed backend timeout. Runtime proof/fencing and timeouts are unchanged. Fresh Provider cohort, broad/historical gates, packet selector-change/selective priority, GHCR application consumption and reproducibility remain OPEN; Phase D BLOCKED.

Run [38181603796](https://github.com/Coxf0rd/fwrouter/actions/runs/38181603796), `fa0056e`, passes the complete current packet scenario: **1 PASS / 0 FAIL / 0 SKIP** (JUnit suite 60.898 s). Native DNS/53, four LAN UDP/TCP answers, external-DNS capture/NXDOMAIN, resolved user TCP/UDP flows, exact nftset materialization and unchanged Core/native selection are proven; all eight disallowed DIRECT capture deltas are zero. Upstream router DNS policy remains explicitly DIRECT, separate from mandatory user VPN forwarding. Packet/native cleanup and runner host snapshots pass unchanged. Preflight: 46 gate / 93 acceptance / 38 network-fixture contracts. [Structured evidence](audits/test_architecture_phase_d_2026-10-09/PACKET_CHECKPOINT_38181603796.json). This supersedes older DNS-pending statements only for the measured global-VPN scenario. Selective-mode domain DIRECT priority, actual selector-change packet proof, two concurrent re-entry failures, GHCR application overlay, fresh broad/historical gates and reproducibility remain OPEN; Phase D **BLOCKED**. No production, deploy, Phase E, CD or L7 changes.

Run `38181140700` (`7a3cce1`) passes preflight and completes the LAN DNS, external-DNS capture and resolved TCP/UDP request operations. The exact router INPUT allowance resolves the TPROXY fixture confinement issue. The case remains **0 PASS / 1 FAIL / 0 SKIP** (JUnit suite 59.803 s): its diagnostic receipt exceeds the unchanged 4,096-byte limit before final DNS assertions. The source-only next correction compacts duplicate diagnostic projections while retaining all native/capture and protected-traffic assertions unchanged. No overall packet acceptance is claimed until the final assertions run.

Run `38180479529` (`5d6a8ed`) passes static/bootstrap and the corrected fixture contracts, native DNS setup, all four LAN DNS UDP/TCP queries, and external-DNS capture/NXDOMAIN. The packet case remains **0 PASS / 1 FAIL / 0 SKIP** (JUnit 62.748 s) at `/probe/dns53-flows` HTTP 502, before final forwarding/parity assertions. The exact resolved-flow failure stage is not yet established; observed tunnel packets are not alone proof of successful endpoint delivery. A bounded fixed-label/stage diagnostic is being prepared. Previous client DNS-guard omission is corrected in the test namespace only; no production changes.

Run `38179605671` (source `6d1e267`) confirms the RD+AD fixture correction in native DNS setup: status HTTP 200, daemon/config validation, UDP/TCP capture and both exact nftset targets pass; owned upstream reply counters increase. Preflight passes 46 gate / 92 acceptance / 35 network-fixture contracts. The full packet case remains **0 PASS / 1 FAIL / 0 SKIP** at LAN client `/probe/dns53` HTTP 502; subsequent external-DNS and resolved-flow checks are NOT RUN. Source review identifies a separate fixture guard omission: new client UDP/TCP port 53 traffic to the router LAN address has no permit under default-drop output policy. A narrowly scoped fixture correction and bounded per-query diagnostic are being prepared; their packet-level confirmation remains OPEN. No production firewall/DNS changes.

Run `38179142372` confirms the owned DNS fixture rejects a real dnsmasq query on `flags_unsupported`: observed UDP header flags **0x0120 (RD+AD)**, while the fixture required exact 0x0100. This is a fixture interoperability defect; AD on queries is defined by [RFC 6840 §5.7](https://www.rfc-editor.org/rfc/rfc6840#section-5.7). Preflight passes 46 gate / 92 acceptance / 35 network-fixture contracts; packet acceptance remains **0 PASS / 1 FAIL / 0 SKIP**. The minimal fixture correction accepts normal RD and RD+AD requests while keeping response AD clear and other validation intact. Its native DNS setup was confirmed by run `38179605671` above; full LAN DNS/packet acceptance remains OPEN. No production DNS/routing policy changes.

Phase D remains **BLOCKED**. Run `38178054841` executes both re-entry cases: **0 PASS / 2 FAIL / 0 SKIP** after successful preflight. Both exact native calls are member-delay HTTP 504 (`mihomo_timeout`), with no integer delay result and adapter result false. The local HEAD/204 write is not correlated to that request; no remote DOWN or successful probe is inferred. The 1500 ms native probe budget and separate 3.5 s HTTPX deadline remain unchanged.

Run `38178387432` reaches the DNS packet phase but fails its status-read timeout. Endpoint receipts show one received `vpn.test` UDP request during manual apply and one received `direct.test` UDP request during status inspection, with **zero successful reply writes**. This narrows diagnosis to the owned DNS endpoint response path; the exact rejection predicate remains unknown. DNS packet/native parity is not accepted. Source `0fda575`'s fresh committed-artifact correction passes its eight exact hosted regressions; subsequent diagnostics do not bypass native proof. Raw evidence remains local and untracked. Full functional/affected, selector-switch packet proof, application GHCR overlay, fresh historical-ID confirmation and reproducibility gates remain open. No production, deploy, Phase E, CD or L7 changes.

## Previous targeted checkpoint — 2026-10-12

Phase D remains **BLOCKED**. Run `38159841586` executes the exact two hosted native re-entry cases after bootstrap passes (gate contracts 46/46; acceptance contracts 89/89); both fail native verification (0 PASS / 2 FAIL / 0 SKIP). At each barrier, an owned HEAD/204 response intent and separate local `end_headers()` completion are observed, but neither proves peer receipt. Mihomo's fresh `extra[test_url]` sample has `alive=false`, delay 0; run evidence does not attribute the writer or cause. Run `38159521000` identifies a stale cached-`None` effective-artifact read blocking DNS53 restart. Run `38160582405` passes DNS-focused bootstrap 8/8 and reaches the native dnsmasq fixture, but readiness times out (0 PASS / 1 FAIL / 0 SKIP); pinned config check and daemon startup pass. Its outbound capture shows `198.18.240.1 → 1.1.1.1:53/UDP`, matching the isolated endpoint's exact DNS input allow rule, but endpoint arrival/reply was not observed. A source-only harness update now uses uncached selective status and records successful endpoint DNS sends separately from received requests; this source is not yet hosted-verified. DNS packet acceptance remains open, and earlier packet PASS receipts remain scoped evidence, not full DNS acceptance.

GHCR publisher and independent anonymous digest-pinned consumer are now verified: run `38152526883` cold pull 28.042 s, warm pull 0.060 s, dependency image 2,598,192,591 bytes. This supersedes only older publisher/consumer-pending statements; application source-overlay migration and Docker-root headroom acceptance remain OPEN. Full functional/affected gates, current seven-ID recheck, packet proof after an actual selector switch and reproducibility remain OPEN. No Phase E, CD, L7, production deployment or runtime changes. See the Phase D REPORT for source/run/evidence boundaries; historical checkpoints below are retained.

## Verified protected-DIRECT checkpoint — 2026-10-11

Run [38141202543](https://github.com/Coxf0rd/fwrouter/actions/runs/38141202543), `cf62b37`: **1 PASS / 0 FAIL / 0 SKIP**, 46.575 s. Real protected TCP/UDP forwarding succeeds under stable active VPN with exact endpoint/capture/native rule/set/counter proof. The prior UDP/empty-pool/Emergency Direct/re-entry checks also pass. Run `38140783732`, `49d40ff`, separately confirms **3 application/native/browser PASS** (including real CAS-miss fresh reconciliation) plus 18 read-model and 4 alias cases. This supersedes the older protected-DIRECT and targeted CAS/browser pending notes only in the tested scope. DNS/53, actual selector-change packet forwarding, provider cohort, full functional/affected, GHCR and reproducibility remain open. Phase D **BLOCKED**; production unchanged. See Phase D REPORT and `PACKET_CHECKPOINT_38141202543.json`; older evidence is retained.

### Packet acceptance update — 2026-10-11

Run [38138693984](https://github.com/Coxf0rd/fwrouter/actions/runs/38138693984), `8689e23`: **1 PASS / 0 FAIL / 0 SKIP**, 55.400 s. Actual supported/unknown VPN UDP paths, unsupported/unavailable/empty-pool fail-closed, TCP, Emergency Direct and verified VPN re-entry pass. Re-entry provider calls are 1→1 (zero additional calls); native handoff/rollback receipt proof and cleanup pass, with unchanged host network/firewall snapshots. This supersedes the pending alias acceptance below only for this packet scenario. DNS/53, protected DIRECT, packet proof after a real selector change, fresh application/browser/provider/full gates and GHCR remain open. Phase D **BLOCKED**, production unchanged. See the [structured checkpoint](audits/test_architecture_phase_d_2026-10-09/PACKET_CHECKPOINT_38138693984.json).


## 2026-10-11 — current bounded UDP/recovery checkpoint

Phase D remains **BLOCKED**. Tri-state UDP and mandatory-VPN fail-closed source are implemented on the CI branch; production is unchanged. Run [38136295650](https://github.com/Coxf0rd/fwrouter/actions/runs/38136295650), `b2ca8d9`, passed **48 exact unit/component cases / 0 FAIL / 0 SKIP** (5.208 s), including native-proof receipt ownership and honest incomplete rollback outcomes. Packet run [38136831776](https://github.com/Coxf0rd/fwrouter/actions/runs/38136831776), `bda38c9`, remains **0 PASS / 1 FAIL / 0 SKIP**. Its trace proves failed Mihomo handoff binding/readback because `172.18.0.1` is absent from the router namespace; receipt publication correctly is not reached. The namespace-only correction `62ef4a4` awaits hosted acceptance. Scoped packet phases do not constitute complete packet acceptance. Port-53 DNS-policy proof, fresh full functional/affected/historical gates and GHCR publisher/consumer remain open. Dated DRAFT/NOT RUN text below is historical and superseded only within the stated source/unit scope; Phase E/CD and the operational branch remain untouched.


### 2026-10-11 — Historical VPN UDP failure checkpoint (pre-approval)

**Historical wording; superseded for the approved source/test scope below.** Phase D remains **BLOCKED**. Run [38079315167](https://github.com/Coxf0rd/fwrouter/actions/runs/38079315167), source `12d40cc`, passed kernel preflight, DIRECT packet path, captures/export and cleanup; its VPN UDP packet assertion failed (**0 PASS / 1 FAIL / 0 SKIP**). Core classified 3 VPN UDP packets and handed all 3 to full-VPN TProxy; TCP reached the synthetic VPN endpoint, while UDP and DNS were observed from router-WAN peer `198.18.240.1`. Native config identity matched; Mihomo readback showed one VLESS proxy with UDP capability false. Provider capability absent from parsed VLESS material is unknown, and native capability projection does not prove remote unsupported status. Transport capability and routing policy remain separate; no application/routing change is authorized pending an explicit tri-state provider capability contract and scoped mandatory-VPN behavior decision. See the [Phase D report](audits/test_architecture_phase_d_2026-10-09/REPORT.md#vpn-udp-contract-checkpoint--2026-10-11). No production changes.

### 2026-10-11 — Approved VPN UDP contract (source only)

The user approved scoped source and isolated acceptance changes for tri-state UDP capability and mandatory-VPN UDP failure handling. This supersedes only the earlier pending-approval status; it does not authorize production changes. Provider declaration is `supported` / `unsupported` / `unknown`, with absent information as `unknown`. Provider declaration, native transport ability to attempt UDP, and actual remote packet proof are separate facts. Unknown capability may be attempted through VPN only if the selected native protocol/transport supports it; otherwise traffic is rejected without DIRECT fallback. Capability absence alone does not mark a proxy DOWN.

Routing policy is independent: mandatory VPN selectors are FWRouter-owned and terminate at `REJECT` when no eligible VPN candidate exists; implicit DIRECT is not a candidate. Paired UDP guards prevent native rule exhaustion or an unavailable endpoint from creating DIRECT fallback. Existing explicit DIRECT exceptions, protected DIRECT, confirmed Core Emergency Direct, and ordinary DIRECT semantics remain intact. DNS remains a separate policy: the existing configured host-resolver DIRECT output is not itself a leak and is not silently changed to VPN. Port-53 DNS policy needs separate packet proof; port-5353 payload acceptance is insufficient.

Native evidence must include configuration/receipt identity, runtime incarnation, loaded Mihomo readback, and packet-level captures/counters/endpoint observations. Hash and selector parity do not prove per-flow rule/sub-rule behavior. Source is a **DRAFT**, tests and packet acceptance are **NOT RUN**, production is unchanged, and Phase D remains **BLOCKED**. Do not close Phase D until all mandatory gates pass. See [Mihomo contract map](PROJECT_MAP/MIHOMO.md#approved-udp-contract--2026-10-11-source-draft-acceptance-pending).

Updated 2026-10-09: Test Architecture & CI Stabilization is active. Phase A delivered; Phase B accepted in bounded source/local-test scope; Phase C source complete. Phase D implementation was authorized and is present on `stage/test-architecture-ci-stabilization`. Run [37934872853](https://github.com/Coxf0rd/fwrouter/actions/runs/37934872853), head `b580b2e`, completed **BLOCKED**: affected L0–L3 1,112 PASS / 129 FAIL with mandatory L4/L5 absent/incomplete; the 37-case functional cohort finished 23 PASS / 14 FAIL. Qualified Mihomo passed 26/26, isolated Docker Xray 1/1, and minimum application Xray readback 1/1; these scoped passes do not close the affected/functional gates. The prior seven-ID cohort passed 7/7 in run [37904598829](https://github.com/Coxf0rd/fwrouter/actions/runs/37904598829); this run reports five PASS and two skipped/uncollected from those IDs. Phase E remains OPEN. No production deploy/restart occurred; CD remains deferred. See [full checkpoint](audits/test_architecture_phase_d_2026-10-09/REPORT.md), [structured receipt](audits/test_architecture_phase_d_2026-10-09/LAST_FULL_CHECKPOINT.json), and [failure analysis](audits/test_architecture_phase_d_2026-10-09/FUNCTIONAL_FAILURES_37934872853.csv). Host TPROXY and nonlocal handoff remain NOT RUN; run 37934437529 logged `Listener fwrouter-tproxy listen err: operation not permitted`, `Listener fwrouter-full-tproxy listen err: operation not permitted`, and `Listener fwrouter-xray-egress-32579334990b listen err: listen tcp 172.18.0.1:53216: bind: cannot assign requested address`. Functional PASS would not prove these capabilities.


## 2026-10-10 — Phase D isolated kernel dataplane checkpoint

Phase D remains **BLOCKED**. The existing Compose harness now has an explicit, separately qualified kernel profile: Debian 12 userland, fixed signed package snapshot, a read-only container, exactly `CAP_NET_ADMIN`, fresh tracked commit source, copied canonical dataplane scripts with SHA/mode proof, and a network namespace proven distinct from the hosted runner. Run [38035140899](https://github.com/Coxf0rd/fwrouter/actions/runs/38035140899) passed real nft syntax/TPROXY apply/readback, Linux policy-route readback, baseline nft preservation and cleanup. This ran **zero application tests**; it proves kernel primitives, not Core recovery or packet forwarding.

Run [38035930948](https://github.com/Coxf0rd/fwrouter/actions/runs/38035930948) passed kernel preflight but returned **0 PASS / 2 FAIL / 0 SKIP** in the two Core recovery scenarios. The application worker omitted `/usr/sbin` from PATH despite native nft existing there; a scoped harness correction is prepared. The independent ordinary selector really selected/applied and advanced revision while a provider operation held its probe; the older provider generation reported `XRAY_GENERATION_STALE_BEFORE_PUBLICATION` internally and a generic provider verification failure externally. This is not evidence of a successful overall race acceptance; the exact fence, last-good persistence and final native target still require assertion and hosted confirmation.

Run [38036768080](https://github.com/Coxf0rd/fwrouter/actions/runs/38036768080), source `37373a6`, then confirmed **2 PASS / 0 FAIL / 0 SKIP**: real Core VPN→Emergency Direct→same-member VPN re-entry and permitted independent ordinary selector competition with exact stale-generation rejection, last-good member/revision and winning native target preserved. This supersedes the two-node failed assertions above, without superseding the failed broad gates.

The next bounded work reuses this harness for one LAN/router/local-endpoint packet testbed, internal networks, fixed `.test` fixtures, explicit egress guards and actual TCP/UDP/DNS/native/counter/capture evidence. Container Native, Kernel Dataplane and Debian Host acceptance are distinct. Compose does not establish Debian host systemd/reboot/physical-interface/production-kernel parity. GHCR publisher source exists but publication/immutable-digest consumption and cold/warm measurements remain unverified. Full functional/affected/reproducibility gates and the historical seven-ID recheck are still open; their earlier failed broad checkpoints remain authoritative until repeated. L7 remains separately authorized manual/release-only. Phase E/CD are not started; production and the operational branch are untouched.

See [Phase D report](/srv/fwrouter/knowledge/audits/test_architecture_phase_d_2026-10-09/REPORT.md), [kernel receipt](/srv/fwrouter/knowledge/audits/test_architecture_phase_d_2026-10-09/KERNEL_PREFLIGHT_CHECKPOINT_38035140899.json), and [testbed contract](/srv/fwrouter/tests/acceptance/NETWORK_TESTBED.md). No new future DNS/configuration/rescue milestones are introduced by this checkpoint.

## 2026-10-09 — Phase C: SOURCE COMPLETE / EXECUTION PENDING

Phase C mandatory source gaps from checkpoint `c25ef4c` are implemented and registered: 31 application definitions / 48 cases (37 functional L3, 11 explicit L7), actual Xray generation/checkpoint/native proof, Core/provider/Mihomo recovery/fences/CAS, SQLite migration/rollback and real Chromium UI→API→state/error tests. Local 60 pure harness/gate contracts PASS; hosted native/browser/crash cases are **NOT RUN — pending Phase D**. This does not close full application acceptance. Seven historical failures remain OPEN. Phase D/E are not started and require separate user authorization. Operational `6004400` remains PAUSED/BLOCKED; no workflow, deploy, runtime or application behavior changes.

See [source completion report](/srv/fwrouter/knowledge/audits/test_architecture_phase_c_2026-10-08/REPORT.md), [exact traceability](/srv/fwrouter/knowledge/audits/test_architecture_phase_c_2026-10-08/TRACEABILITY_MATRIX.csv), [checkpoint history](/srv/fwrouter/knowledge/audits/test_architecture_phase_c_2026-10-08/CHECKPOINT_C25EF4C_REPORT.md) and [prior roadmap](/srv/fwrouter/knowledge/history/CANONICAL_ROADMAP_PRE_PHASE_C_EXTENSION_2026-10-09.md).


## Current evidence and boundaries

### Xray generation recovery — scoped complete

Source: `001c6e9`, `95f54c1`, `5728d85`, `a139e66`. Tests: historical raw run 1,322 passed, 52 failures with the same IDs as the `58e053a` comparison, 1 skipped; relevant cohort 603 passed, 23 failures, 1 skipped. These failures are not approved CI exceptions. See the durable current [baseline status](audits/test_architecture_cicd_2026-10-04/BASELINE_STATUS.json) for exact IDs and triage. Separate pinned Mihomo, actual Xray archive/readback, and public-wrapper/idempotence gates passed. Deploy: standard backend/docs workflow; API startup performed owned recovery. Live: current 78-identity generation was verified through current DB/public/binding/native readback and checkpoint receipt/closure; a later ordinary refresh was a verified no-op. A changed-input live generation was not forced. The October 2 initial exception cause remains unproven. One historical subscription-refresh unit failure predates deployment; its next scheduled run was not observed. See the [dated acceptance report](audits/xray_generation_recovery_2026-10-04/REPORT.md).

### Provider-managed source and protocol foundation

Current source stores provider bindings, credentials, observations and applied state per `source_ref`; it is not a singleton `.env` provider setting. Eight protocol profiles and mixed per-entry dispatch are implemented, tested and deployed. Current-account live evidence covers the observed Hysteria2 path; seven other provider handshakes remain unverified. Emergency Direct/provider API evidence correction is implemented and deployed; real fault/re-entry acceptance remains a Stage 9 gate. See the [current provider contract](PROJECT_MAP/PROVIDER_MANAGED_SUBSCRIPTIONS_AND_ADAPTERS.md); previous singleton/env wording is preserved in [dated history](PROJECT_MAP/history/PROVIDER_SPEC_PRE_2026-10-04_RECOVERY_RESEQUENCE.md).

### Test Architecture & CI/CD — historical foundation, current acceptance boundary

Foundation `178b471` classified149 files and delivered taxonomy/manifest/fixtures/selection/smoke/workflow sources. Its old eleven-last-failed and remote-provisioning-open wording is preserved in the [exact pre-consolidation mirror](history/ROADMAP_PRE_2026-10-13_PRODUCTION_READINESS.md), not a current failure count. PhaseD38305978196 now confirms1429/0/0 and historical exact-seven7/7, native/application/browser/kernel/packet proof. PhaseE technical checks passed in their report scope; actual PR/main/settings gates remain BLOCKED. L6/L7/full Debian host release acceptance are NOT RUN; protected deployment integration is DEFERRED CD, not a CI implementation prerequisite. Keep original [foundation report](audits/test_architecture_cicd_2026-10-04/REPORT.md)/[baseline record](audits/test_architecture_cicd_2026-10-04/BASELINE_STATUS.json); no blanket exceptions. Follow affected L0–L5, separate L6 policy and authorized L7 gates; durable evidence belongs in knowledge/audits, not only /tmp.

## Active execution order

Only this sequence is active. Dated entries below record evidence at the time and do not define today’s plan. Prior wording is preserved in [history](history/README.md), including the [exact canonical snapshot before this resequencing](history/CANONICAL_ROADMAP_PRE_2026-10-08_CI_STABILIZATION.md) and [the former English mirror](history/ROADMAP_PRE_2026-10-08_CI_STABILIZATION.md).

1. **Test Architecture & CI Stabilization — PRE-MERGE ACCEPTED / READY FOR USER MERGE subject to latest protected PR5 checks.** PhaseA/B complete, PhaseC complete in agreed scope, PhaseD hosted ACCEPTED. Actual PR38347054113 passed1429/0/0 plus qualified26+1, ordinary26/Provider11/packet1 and historical7/7. Required stable Actions check, squash-only and auto-delete configured with no bypass. Only the user performs Squash and Merge; actual main-push verification then completes the milestone. No agent merge/deploy/CD, no production or Operational branch changes. [Final handoff](/srv/fwrouter/knowledge/audits/test_architecture_phase_e_2026-10-13/FINAL_HANDOFF.md).
2. **Operational Performance Fixes — PAUSED/BLOCKED.** Preserve local branch `stage/operational-performance-fixes` at `6004400`; do not transfer its commits into the CI milestone branch. Packages 1–2 contain implemented changes and bounded deployment evidence; Package 3 is unimplemented and conditional. No PR/merge until full application staging acceptance. After the CI milestone is accepted through the repository review process, return to the operational branch, integrate the resulting current `main`, run mandatory full staging CRUD/crash/restart/recovery acceptance, then verify correctness and comparable performance. Close the milestone and authorize a separate PR/manual merge only after PASS. On failure, retain BLOCKED and enumerate the exact gates.
3. **Stage 5 — configuration and persistence contracts.** Start after Operational Performance Fixes is accepted. Define env/SQLite intent/generated artifacts, installer, clean install, backup/restore/rollback and supported upgrade/release contracts.
4. **Database Architecture & Integrity Audit.** Tables/relations, canonical/derived/cache data, constraints, indexes/scans, stale/orphan rows, transactions/WAL/CAS, writes/growth/contention, migrations and trigger candidates.
5. **Evidence-based Database fixes.** Only issues proven by the audit; Core/runtime/network/selector/recovery/Health/provider/Mihomo/Xray ownership stays out of triggers.
6. **Stage 6 — dead/obsolete compatibility and security/data-handling cleanup.**
7. **Stage 6A — measured internal execution cleanup.** Reuse Stage 4 evidence; do not create an independent performance backlog.
8. **Stage 8 — Core ↔ Modules contracts** for ownership, API/events/config/lifecycle/Health.
9. **Stage 7 — physical module extraction** only after Stage 8.
10. **Post-extraction functional/architecture audit**, followed by planned External Integration Framework & SDK after DB/Core/Modules stabilization.
11. **Hardening and final runtime/config/deployment stabilization.**
12. **External Telemetry Ingestion / Metrics and Traffic Accounting — a late, separate milestone** after extraction and DB/architecture stabilization. FWRouter exports domain evidence only; host/container metrics belong to a separate observability project. Do not prepare future-version schema or analytics in advance.
13. **Final UI redesign.**
14. **Production Documentation**, including full technical documentation for the accepted test/CI system.
15. **Stage 9 — disposable staging failure/recovery, Configuration Matrix and separate Debian host acceptance.**
16. **Stage 10 — final release audit and mandatory Production Live Acceptance after separately authorized deployment.**

**CD is outside this execution order and deferred until a separate decision.** Until then, production deployment remains manual through the existing installer: `/srv/fwrouter/installer/install.sh --deploy --component backend --component ui` (add `--component docs` for documentation changes). Deploy mode copies selected components and does not restart services. When backend changes require applying, restart the API separately with `systemctl restart fwrouter-api.service`; docs-only deployment needs no restart. Future CD is a separate project requiring a protected environment/approval, exact-commit promotion, readiness/Health/native parity/smoke and proven rollback; GitHub Actions must not automatically deploy production now.

## Test Architecture & CI Stabilization — Phase E final promotion

Status: **PRE-MERGE ACCEPTED at PR5/run38347054113**. Latest protected HEAD must pass before user merge; post-merge main event remains pending. Previously blocked real PR/settings gates are now configured/verified; no production acceptance implied. Phase A/B are complete, Phase C is complete in its agreed scope, and Phase D is ACCEPTED. [Phase E report](/srv/fwrouter/knowledge/audits/test_architecture_phase_e_2026-10-13/REPORT.md), source `5decb99`, records technical audit/corrections: affected selection, L0–L7, reproducibility, real application/native/browser proof, packet-level testbed, isolation/cleanup/failure handling, historical regressions, false-PASS/SKIP rejection, GHCR publisher/consumer and fresh-source verification. The wrong PyPI `backend` wheel was replaced by verified local `fwrouter-api` with all247 Python source files matching checkout. No new execution is claimed by this documentation update.

| Event | Required purpose |
|---|---|
| Feature push | Fast affected L0–L1 |
| PR → main | Affected L0–L5; native/browser where selected by dependencies |
| Main push | Applicable affected checks, integration/native smoke and build identity |
| Nightly/manual | Separate L6 full regression policy |
| Manual/release authorization | Separate qualified L7 failure/recovery gate |
| Dependency input changes | Trusted GHCR publisher; read-only digest-pinned consumers |
| CI diagnostic branch | Explicit targeted diagnostics; no unmarked duplicate expensive jobs |

**Pre-merge acceptance:** separately authorized real PR event, stable mandatory status context `Affected L0-L5 with hosted profiles`, authorized required-check/branch-protection and squash policy configuration; required jobs present and fail closed on missing proof, infrastructure errors or unjustified SKIP. Isolation is production-independent; independent repeats and exact native/packet/application receipts remain required. GHCR consumes current checked code, not stale application layers. Audit workflow/job duplication and measured wall/CPU/RAM/disk/I/O/image costs; retain unmeasured dimensions explicitly, optimize only proven waste.

**Post-merge verification:** after separately approved operator Squash and Merge, verify the actual main event and its required checks. Main-event proof is not a prerequisite to the merge that produces it. Pre-merge proof is now accepted at PR5; the latest protected head must pass before user merge. Final post-merge milestone completion remains OPEN until the main-event sequence is verified. Branch-equivalent/manual checks do not prove PR/main events. New default-branch/manual publisher events retain their own unverified boundary. Residual L6/L7/release/host gates are documented separately; future configuration/DNS work does not reopen accepted Phase D or become an artificial Phase E blocker.

Current authorized policy checkpoint (2026-10-13): [PR5](https://github.com/Coxf0rd/fwrouter/pull/5) real event38347054113 PASS; repository squash=true, merge/rebase=false, auto-delete=true. Existing active Main ruleset now requires `Affected L0-L5 with hosted profiles` from Actions15368 with strict up-to-date policy and no bypass. Earlier target/NOT RUN/settings-authorization wording in this section is a pre-PR checkpoint superseded by [PhaseE final handoff](/srv/fwrouter/knowledge/audits/test_architecture_phase_e_2026-10-13/FINAL_HANDOFF.md). Post-merge main remains NOT RUN and follows user merge; no deploy/CD authority.

## Development workflow — Git and release policy

One independent task/milestone → one feature/fix branch → one reviewed PR → **one squash commit in main**. Source/tests/docs/roadmap travel together; intermediate commits are allowed. Start the next independent branch only from updated main after confirmed merge. Required CI PASS precedes operator-approved PR merge; agent PR creation and merge need separate authorization. Direct main push is restricted; emergency-hotfix exceptions need a separately defined policy. Squash creates a single-parent commit, not a two-parent merge commit. Preserve engineering decisions/evidence in durable docs or the PR before deleting completed merged branches; unfinished branches are untouched.

| Repository setting | Target / last recorded state |
|---|---|
| Allow squash merging | ON / already ON |
| Allow merge commits | OFF / now configured with user authorization |
| Allow rebase merging | OFF / now configured with user authorization |
| Automatically delete head branches | ON / already ON |
| Main protection/ruleset | PR + strict up-to-date required stable Actions15368 check; now configured |
| Direct push to main | Restricted; no ordinary bypass |

These settings are now configured under the final closure authorization; no unrelated access settings changed. Historical retain-branch wording is superseded. `/AGENTS.md` is unchanged; any later developer-instruction synchronization is a separate task.

CI → authorized PR review/required checks → operator Squash and Merge → post-merge main verification → Release Candidate → applicable release acceptance → independently approved manual deployment → Production Live Acceptance. CD **DEFERRED**; merge/CI PASS/PR approval never authorizes deployment.

## Test policy for every subsequent implementation milestone

Reports list the test levels actually run and why. Default to affected L0, affected L1/L2 and required L3/L4; use L5 when shared/domain contracts change. L6 is not the default; run it only at a scheduled/manual/milestone/release/policy gate. L7 is separate staging/release acceptance after compatibility of the isolated environment has been confirmed. A plan, dry-run or partial selection is not a passing gate.

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

## Stage 6 / 6A — cleanup and measured execution

Preserve Stage6 dead/obsolete compatibility/security cleanup and Stage6A measured internal execution cleanup; use Stage4 evidence rather than a second performance backlog. The server-wide maintenance item below is separate from application-code cleanup.

### Server-Wide Cleanup & Storage Audit — PLANNED

Scope: the entire Debian12 server named `minis` in this request (earlier evidence calls it `minisk`; verify identity before any future operation), not only FWRouter. Priority: pre-release maintenance after component stabilization, final action near release; read-only inventory may accompany Stage6. Inspect disk allocation, backups/snapshots, duplicate configs, Docker images/containers/volumes/build cache, systemd journal, app/debug logs, temp files/dumps, dev/test artifacts, staging/deployment copies, generated files and diagnostic retention.

Classify each object **keep / archive / delete after confirmation** with owner, purpose, dependencies, restore method and retention requirement. Protect production DB/active volumes/current backups, Rescue, certificates/keys/credentials, Home Assistant/Vaultwarden, historical audit reports/raw investigation evidence and current CI/release artifacts. No blanket `docker system prune -a --volumes` or deletion without separate reviewed authorization.

Entry: stable release candidate and storage/backup ownership map. Acceptance: approved deletion candidates, independent rollback/restore, measured space before/after, service/data integrity proof and justified log/backup retention. Evidence: inventory/classification, approvals, backup/restore receipt and bounded before/after measurements. No cleanup is authorized by this plan.

## Stage 8 → Stage 7 → Post-Extraction Audit

Keep Core↔Modules ownership/API/events/config/lifecycle/Health contracts before physical extraction. Post-extraction audit must verify those contracts, app/native parity, migrations, failure ownership and resource behavior. Do not introduce distributed infrastructure without evidence.

### External Integration Framework & SDK — PLANNED, late

Entry: DB/Core/Modules contracts and physical extraction/post-extraction audit stabilized. Define versioned integration API/events/config/lifecycle/authorization, ownership and supported SDK contracts without duplicating Core authority. Acceptance: compatibility/negative/isolation contracts, documented example against real supported APIs and evidence-backed versioning. Public SDK documentation belongs to Production Documentation after implementation. This remains separate from late Metrics Ingestion/Traffic Accounting and future-version UI/analytics/schema work.

## Hardening — Network/DNS Reliability & Security

Entry: configuration/persistence and relevant Core/Modules interfaces stabilized, before final production release. Existing Stage9 validates failure behavior. Neither DNS optimization nor expanded security matrix is an additional current PhaseE blocker without a demonstrated dependency.

### DNS Performance & Reliability — PLANNED

Priority: measured network reliability before release. Recorded infrastructure includes dnsmasq DHCP/DNS/local records/domain rules, FWRouter-generated dnsmasq config, installed BIND9, Mihomo/Xray and nftables routing. These are planning inputs, not a new live inspection; reverify actual configuration/ownership on entry.

Audit full DNS request chain and generated-config ownership; cache behavior, cold/warm p50/p95 latency, hits/misses/upstream traffic, CPU/RAM/disk I/O/background requests, upstream/VPN failures, DNS capture/nft sets and DIRECT/VPN/fail-closed priorities. Compare dnsmasq-only, dnsmasq+BIND9 and justified alternatives; assess cache size, TTL, prefetch, serve-stale/fallback and loop/leak risks. Existing BIND9 is not mandatory: retain/change role/remove only based on measured evidence and reviewed dependencies. Complexity is not performance, and reduced DNS latency alone does not prove faster websites.

Acceptance: DHCP/DNS correctness, preserved routing/fail-closed, no new leaks/loops, comparable before/after performance/resources, demonstrated benefit or confirmed risk removal, justified complexity and ready rollback. Evidence: topology/ownership, bounded query/packet/counter measurements, failure scope and restore plan. No changes now.

### Rescue `rescue0` Network Isolation — PLANNED, HIGH priority safety backlog

Unconfirmed risk: `rescue0` may be classified as ordinary LAN. Do not call it a proven defect. Start with separately authorized safe read-only audit of interface classification, DHCP bindings/exposure, Rescue ownership, DNS/firewall isolation, LAN/Rescue boundaries and FWRouter generated configurations; assess effects on strongSwan/IKEv2/Rescue API and routing conflicts. This safety audit may run earlier than hardening if justified by risk, without reordering implementation milestones or altering management access.

If confirmed, prepare a separate fix with dependencies, independent rollback, protected administrative/rescue access and appropriate isolated/host acceptance. PASS: classification/DHCP/DNS/firewall ownership established with evidence; no unintended LAN exposure or Rescue interference, or a tracked confirmed defect with verified remediation. No Rescue/VPN change authorized here.

## Production Documentation & Open-Source Presentation — PLANNED

Entry: mature UI/API/architecture and supported install/release contracts; before release. Keep `README.md` English and `README.ru.md` Russian with easy switching, concise purpose/features/architecture, verified screenshots and useful badges/diagrams/demos only. Put full navigable user/engineering documentation in `docs/`; keep internal handoffs/agent instructions/architecture working notes separate.

Deliver Installation/Quick Start, Initial Configuration, User Guide, Network/VPN and DNS guides, API Reference, Developer/Contribution guides, Security Policy, Troubleshooting, Backup/Update/Recovery/Rollback, supported platforms/limitations and full Test/CI technical documentation. Add implemented External Integration Framework/SDK docs when available; no unsupported feature claims.

Acceptance: both READMEs usable, coherent navigation, actual UI images, installation/update/recovery instructions verified in supported isolated environments, links/examples working and content matching application behavior. Evidence: reviewed docs/source versions, screenshots and instruction-validation receipts; no production operation merely to validate docs.

## Stage 9 — Recovery/Staging and Debian Host Acceptance

Preserve separately authorized L7 failure/recovery/crash gates and Configuration Matrix validation. Distinguish **Application Acceptance**, **Kernel Dataplane Acceptance** in isolated Linux namespaces and **Debian Host Acceptance**. Docker Compose does not prove host systemd, boot/reboot, physical interfaces, production-kernel compatibility or system-failure recovery.

Host gate: dedicated safe environment with independent console/access/rollback; inspect systemd lifecycle, boot/reboot restore, real interfaces, host routes/nftables/kernel compatibility and recovery. Destructive injection requires disposable qualified host/VM and authorization. If unavailable, record exact NOT RUN scenarios, impact and release disposition; never substitute mock/Compose PASS. Required evidence: environment qualification, boot logs, native route/state/packet proof and cleanup/recovery receipts.

## Stage 10 — Release / Production Live Acceptance

Entry: reviewed RC, applicable CI and separately authorized L6/L7/manual/host release gates; no automatic deploy. Prepare a safe production baseline, independent tested rollback and confirmed Rescue VPN/SSH/admin access. User approves exact deployment separately. Use the existing documented installer; deployment and needed managed restarts are explicit operations, not a consequence of PR approval/merge.

After deployment require Core desired/applied/native parity; Xray/Mihomo and bindings; DIRECT/VPN/DNS/DHCP; API/UI/local services; routes/resources/errors and bounded stability/no-oscillation observation. Capture exact deployed source/schema/config hashes, health/readiness/native/packet evidence, provider counters and access checks; rollback if critical invariants fail. Protect newer intent/data; no blind old-DB restore or provider mutation. No production destructive failure injection.

**CI Acceptance ≠ Production Live Acceptance.** Release acceptance uses safe isolated fault tests; live acceptance verifies the installed server after separately approved deployment. PASS requires healthy verified convergence, unchanged protected access and explicit treatment of residual host/manual gates. CI PASS, PR approval and merge never grant deployment authority; CD remains DEFERRED.

## Performance and observability boundary

Stage 4 is a read-only baseline of the current release: CPU/RAM; API latency; SQLite query latency/count and WAL/DB I/O; SSD writes/storage growth; polling/timers/jobs; logging volume; duplicate requests/work; process/container overhead; background wakeups; adapter/runtime cost. Fix only measured bottlenecks and require comparable before/after measurements for each fix. Stage 6A consumes the same evidence. Preserve a minimal CPU/RAM/SSD footprint. A separate server-metrics project may collect host/container resources; FWRouter should not duplicate that collector.

## Preserved open work

- Xray changed-input live acceptance and the next natural scheduled refresh remain open; a prior systemd refresh unit failure is historical and was not reset or retried to obtain a green status.
- Hosted Xray external-client application CRUD/native acceptance is confirmed in agreed PhaseD scope, not every External Connections registry/integration path. Remaining registry/Rules typed-audit coverage needs exact scoped confirmation.
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


### Phase D factual checkpoint — 2026-10-12
Provider cohort run38208049662:11/0/0, both reentry fencing cases confirmed. GHCR ordinary application consumer run38210552876:1/0/0 native application plus historical exact seven7/0/0; fresh-source overlay build9.307s and anonymous pull28.411s with actual DockerRoot boundary measurements. Kernel/packet/recovery retain pinned legacy build. New selective packet extension currently fails native mode/default apply verification (run38210175218); target-switch packet proof not reached. Full functional/affected/reproducibility gates remain OPEN; PhaseD BLOCKED. No production changes/PhaseE/CD/L7. Detailed scope and evidence: PhaseD REPORT.
