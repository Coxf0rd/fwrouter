# API runtime scale and lifecycle stabilization

## Status and boundary

Source work in `fix/api-runtime-health-scale-stability` remains under qualification in draft PR8. Installed backend is the user-authorized controlled candidate4ffd686; its120s readiness failed and API/dependent workers/timers remain held. Current authorization is read-only production diagnosis only. No further deployment, unit start, native configuration apply, identity restoration or unsafe old rollback is performed. Historical source/NOT RUN checkpoints below are retained in order.

PR7 is merged as `1b302e91b3cd652cd4aab4f45bbdd88c088e5786`, parent a4a7a33, tree equal to accepted acfbd8a. Actual main push [38400414256](https://github.com/Coxf0rd/fwrouter/actions/runs/38400414256) succeeded:217 routine cases,26 qualified child cases, native main smoke, aggregate eligible with no missing levels or blocked reasons. Source wheel audit has no errors. This scoped CI is not production stability acceptance.

## Evidence and causes

Historical live evidence remains in the preceding Live Acceptance report and protected recovery set. Startup rose from31.129s to approximately254s;15-minute observation had16/31 health timeouts; API RSS reached6056568KiB; stop hit20s host deadline. No shutdown stack was captured, so the complete stop delay cannot be uniquely attributed.

1. Captured startup and steady stacks prove full YAML parsing during native health, including selector restore/Xray reconciliation. The mounted document is20,754,596 bytes with199,257 top and199,257 transparent rules. PR7 replaces the health parser with a safe C loader; its9.1x parser-only result does not solve duplicate native observations.
2. Full health holds YAML and native JSON graphs and formerly also two complete normalized tuple arrays. It read the full configuration twice. Streaming ordered pair comparison and a single config snapshot remove these confirmed extra allocations without weakening native proof.
3. Runtime/dataplane shared health had a two-second singleflight cache, but Mihomo status, StateSnapshot and presentation selector state bypassed it. Concurrent requests/prewarm therefore duplicated expensive work. They now share the existing cache, keyed by adapter identity. Mutation, selector changes, restore and publication proof retain fresh direct health; the selector API presentation route explicitly opts into caching. Non-mutating internal selector reads remain fresh by default. Existing invalidation hooks remain active. Read projections explicitly tolerate at most the existing2s completed-snapshot lifetime.
4. Checkpoint mandatory-route and rollback-policy parsers still used Python SafeLoader. They now use CSafeLoader with safe fallback, retaining malformed/nonmapping/constructor rejection. No routing or rollback-policy changes.
5. Six scheduler stop functions discarded an alive Thread after join timeout; a new start could clear its stop Event and create duplicate work. External collector lacked equivalent ownership lock discipline. All seven retain ownership until exit.
6. Shutdown formerly issued stop signals sequentially with separate2s joins and had no prewarm owner stop. It now signals all owners before a shared14s maximum join budget (the former seven2s budgets, not an extended host deadline). Prewarm stops between complete read-only projections and closes admission until the next application lifespan. Partial startup failure also executes cleanup. This does not forcibly cancel native writers or claim that every in-flight operation is cancellable.
7. Native owned `inet fwrouter_v2` retained two obsolete Rescue LAN markers: IPv6 ingress block and DNS-capture allowance. Existing consistency comparison checked only missing expected markers. It now detects reserved-role LAN markers through the existing Core drift/reconcile path. Separate legitimate Rescue NAT rules are not touched. Production removal remains NOT RUN until approved deployment/Core apply.

## Qualification design

Component regressions cover ordered count/index/type/disabled native validation, late mismatch, one config read, concurrent read-only singleflight, adapter separation/invalidation, fresh mutation bypass, seven scheduler stop/restart barriers, prewarm stop/admission and shared lifespan join budget. These are component evidence, not native acceptance.

One new functional hosted scenario uses the existing Debian12/internal-network/GHCR Compose harness. It builds synthetic199257-rule top/transparent sequences over20MB with real mandatory UDP guards, inert reserved test domains, REJECT-only empty VPN selectors and protected explicit DIRECT. Real Mihomo parses/loads the config; actual API startup/bootstrap/schedulers/prewarm and78 synthetic Xray clients run against owned SQLite and native processes. Six concurrent API clients repeat cold cache-expired waves. Budgets: health<5s; Mihomo/selector<15s; runtime<20s; post-ready API RSS<1.5GiB; graceful SIGTERM with observed completed application lifespan<8s. Metrics/counters are bounded and no config/credentials are exported.

This profile explicitly disables host recovery/apply and has no reconciled native receipt: raw native rule/guard parity must be true while full policy_installed remains false. It must not falsely report VPN egress readiness. Host systemd/reboot/native stock Docker parity, full production Xray78-client recovery remain separately unqualified. Isolated process load is not a promise of live performance.

## Execution ledger

| Gate | Status |
|---|---|
| PR7 actual main/source/wheel/native smoke | PASS |
| New source AST/catalog/surface checks | PASS |
| New component tests | NOT RUN — pending hosted PR |
| Large native/API performance scenario | NOT RUN — pending hosted PR |
| Affected native/browser/Provider/packet | NOT RUN — pending hosted PR |
| Production restart/deployment/live acceptance | NOT RUN / BLOCKED |

Updated evidence, metrics, commits and final PR acceptance are appended after hosted execution. CI milestone remains CLOSED; this is corrective production stabilization, not Operational Performance Fixes or another planned stage.

## First PR qualification — 38404505906

[Actual PR run](https://github.com/Coxf0rd/fwrouter/actions/runs/38404505906) failed routine qualification. Hosted profiles are NOT RUN, not PASS. Failures grouped into invalid duplicate native test indexes, outdated no-keyword shutdown mock, prewarm test admission-fixture misuse, and catalog partition/required-native lists not expanded for the new scenario. The fixes preserve assertions and add the scale scenario to shared required-native dependencies. Process-lifetime VmHWM is now collected alongside periodic post-readiness RSS to include preceding startup high-water memory. No production action.

## Native qualification — 38406591732

[PR hosted run](https://github.com/Coxf0rd/fwrouter/actions/runs/38406591732): routine1424 PASS/0 FAIL/0 SKIP; qualified native26+1 PASS; ordinary26 PASS/1 FAIL; Provider11 PASS; packet1 FAIL. Cleanup is owned_resources_removed. Scale failed before config/load measurements: Uvicorn0.34 deliberately re-raises a captured SIGTERM after shutdown (upstream server.py capture_signals). The test now verifies a worker marker emitted strictly after real application lifespan cleanup, plus bounded exit0 or-SIGTERM; an unobserved signal termination is still FAIL.

Packet exclusive-disable readback failed because a non-mutating internal selector proof received the newly cached health. This is a source regression in this branch, not a fixture mismatch. `read_only` controls side effects and must not imply cached proof. All internal selector observations retain fresh native health; only the presentation endpoint opts into cache_runtime. A component regression covers these independent choices, while the existing packet exclusive-disable scenario remains the actual native regression. No fencing/readback checks were weakened.

The pending packet extension seeds the exact two stale Rescue LAN markers in the qualified router namespace, verifies owned drift, invokes real Core API apply, verifies drift removed, and compares an independent Rescue DNS NAT fixture table byte-for-byte. Its source is prepared; execution remains pending. It never touches host/production networking.

## Full startup qualification refinement

The first scale case disabled recovery and therefore could not qualify the complete production startup path. The revised case uses the existing hosted-kernel-dataplane node override, seeds the canonical effective rules artifact and persisted external-client projections, and runs actual bootstrap recovery/reconcile before load. It requires verified generated/native receipt and installed policy, preserves all 78 synthetic external identities, samples API RSS from process spawn, and checks completed application cleanup. An empty subscription-profile reconcile is explicitly not evidence of rebuilding external identities. Host systemd, reboot, stock-Docker host recovery and live deployment remain NOT RUN.

The exhaustive hosted partition becomes ordinary26 + kernel12 + packet1 =39; only the scale node gains the existing confined kernel profile. File-level default isolation is unchanged. Bootstrap diagnostics contain booleans, bounded counts and allowlisted error/status fields, never raw configuration. Source AST/catalog/surface and diff checks PASS; revised hosted startup qualification is pending. Run38412219090 tests the preceding recovery-disabled case and isolated stale-Rescue marker cleanup; it cannot qualify the revised full startup scenario.

## Hosted attribution — 38412219090

Actual PR run: ordinary26 PASS/1 FAIL, Provider11 PASS, packet0 PASS/1 FAIL; cleanup owned_resources_removed. The scale case stopped before startup measurements because native Xray inbound-user JSON for78 synthetic clients exceeded the harness16KiB retained tail. Semantic inbound-user stdout now has a separate256KiB bounded allowance; published diagnostics retain the existing small redacted limits. No startup/RSS improvement is claimed from this run.

Packet qualification demonstrated a source defect: repeated DIRECT detects stale owned prerouting markers but selects classify-only hot swap, which cannot remove them; job reports success while enforcement remains unverified. Global routing now carries its observed owned-artifact drift into pipeline input and disables classify-only optimization for that repair. Full native apply/check/readback remains required; a component regression proves ordinary eligible hot swap still works and drift repair cannot select it. The unchanged packet case remains hosted native regression evidence pending execution.

## Full startup observation — 38442319968

Ordinary26 PASS; kernel cohort11 PASS/1 FAIL; packet1 FAIL; owned cleanup succeeded. Native Xray78 readback now succeeds. Scale worker did not reach readiness within45s, sampled API process high-water621180KiB, and did not complete SIGTERM cleanup within8s while startup was still active. This is not steady-state or complete startup acceptance. Bounded phase enter/exit timings and traceback-only SIGUSR1 collection are added to attribute the blocked startup; budgets remain unchanged.

The stale-Rescue packet extension reached verified full Core apply, zero stale markers and unchanged independent Rescue NAT table, then failed because its new diagnostic phase was absent from the fixed allowlist. Registration is corrected; the remaining complete packet flow still needs execution. Worker logs also expose an owned-worker-survived-lifespan message in a nominally passing Provider browser case; cleanup/lifecycle acceptance is not claimed from test counts alone.

## Startup root cause and complete packet result — 38444740666

Ordinary26 PASS, Provider/kernel11 PASS/1 FAIL, complete packet1 PASS/0 FAIL/0 SKIP. Packet evidence confirms full Core repair removes both stale Rescue LAN markers while preserving the independent NAT fixture, followed by all existing DIRECT/VPN/Emergency/empty-pool/protected/DNS flow assertions. Production rules remain unchanged.

Full startup remains FAIL: live routing recovery15.465s, selector restoration5.5781s, intended/scoped checks0.006/0.0043s. At the45s readiness deadline the actual stack is Xray profile reconciliation -> collect_xray_client_mode_directives -> get_subject_with_effective_state -> default runtime enforcement -> global preflight -> selective rules summary. The common large rules projection is repeated for each external client. The correction shares a fresh operation-local default enforcement snapshot across clients, not a persistent cache; per-client effective modes and later writer/native proof remain mandatory. Peak API VmHWM659692KiB before readiness is partial startup evidence only.

A separate confirmed lifecycle gap leaves mutation-triggered prewarm/jobs outside cleanup when startup schedulers are disabled, and JobManager workers were not drained by lifespan at all. Prepared fix always stops prewarm and drains already-started jobs within the existing14s shared budget, without cancelling native writers. Browser exclusive acceptance now waits for its actual job to finish. Fixture teardown will fail on an explicit surviving-owner lifespan error instead of silently reporting functional PASS. These source changes remain pending hosted qualification.

## Routine fixture correction — 38448003790

Routine gate failed two tests; native profiles NOT RUN. Slow-job API latency test previously measured TestClient lifespan teardown and released its barrier only after shutdown, incompatible with correct job draining. It now measures only the actual HTTP request and releases the operation barrier before context exit, preserving the running-job and2.5s response assertions. The fresh per-invocation Xray policy regression now uses existing isolated_host_observations so actual default preflight does not discover runner host interfaces. No native/host permission is broadened.

Fixture teardown now gives the existing14s application shutdown budget a bounded16s process wait and reports forced termination as cleanup failure. The separate scale graceful-shutdown criterion remains8s. Raw evidence retained privately; production unchanged.

## Measured startup improvement; remaining harness qualification — 38449188257

Ordinary26 PASS, Provider/kernel11 PASS/1 FAIL, full packet1 PASS. Worker reached readiness30.4184s; API VmHWM625324KiB (partial startup/reconcile; not steady-state acceptance); observed completed shutdown5.7764s. Live recovery13.6617s, selector4.6959s, Xray phase8.8514s. The per-client preflight stall is removed, but this run is not acceptance: Mihomo candidate validation was blocked by the harness4MiB limit for a20MiB-scale owned configuration; DNS startup lacked the native DNS fixture in kernel profile. Both failures remain explicit.

Qualification now moves the scale node to the existing packet testbed, which supplies pinned native dnsmasq, private config paths, internal endpoints and real restart/readback. Fixture setup occurs before lifespan bootstrap; normal production reconcile is retained. One reserved vpn.test rule supports the fixture's actual bounded local DNS readiness query, with total99627 VPN domains unchanged. Mihomo candidate cap becomes32MiB only for owned paths, still bounded and still requiring actual native validation. Existing small redacted diagnostics limits are unchanged. Partition is26 ordinary +11 Provider +2 packet-profile cases=39; the scale case is kernel-backed application startup, not a second packet-forwarding proof. Full original packet proof remains mandatory. Hosted validation pending; production unchanged.

## Partition contract correction — 38452205932

Routine gate caught one stale expected provider-node set left from the earlier kernel registration. Corrected it to the exact11 Provider nodes; scale remains mandatory in the two-node packet suite. Seven targeted source-only selection/manifest contracts executed locally PASS; they import no application and execute no native services. Full hosted runtime qualification remains pending. No product assertion or scenario was removed.

## Sequential packet-worker ownership — 38453251674

Ordinary26 PASS, Provider11 PASS, packet core1 PASS and scale setup1 ERROR; no scale metrics were produced. Setup stderr was not retained because pytest setup status is reported only after fixture unwinding. The collector now uses its own setup-complete flag so failed setup always retains bounded redacted logs.

Source review confirms packet-worker native fixture cleanup was in an outer finally after uvicorn.run. Uvicorn0.34 re-raises captured SIGTERM, which can terminate the process before that finally, leaving the owned loopback handoff alias for the next worker. Cleanup now executes within application lifespan before the verified completion marker, including exceptional lifespan exit; outer cleanup remains an idempotent fallback. The actual previous setup stderr is unavailable, so this source-proven ownership defect is not represented as a captured error message. A source-only behavioral context-manager regression PASS verifies application cleanup, native cleanup and marker ordering, plus exception cleanup without false completion. Hosted sequential-worker/native qualification pending. Production unchanged.


## Full native startup attribution — 38456264173

[Actual PR run](https://github.com/Coxf0rd/fwrouter/actions/runs/38456264173): ordinary25 PASS/1 FAIL, Provider11 PASS, packet core1 PASS and scaled startup1 FAIL. Sequential packet-worker setup succeeds after lifespan-owned fixture cleanup. The ordinary failure is Chromium SIGSEGV during browser launch, before UI assertions; root cause remains unknown and no retry or guessed browser workaround is introduced.

Scale sampled API VmHWM739720KiB and did not reach readiness within45s. At deadline the actual stack is garbage collection / JSON decoding of native rules in Mihomo health, called by wait_for_mihomo_controller during staged Xray profile apply. Xray startup phase38.2864s and DNS phase4.8557s are observed; shutdown during unfinished startup exceeded8s. These are partial failure diagnostics, not startup/shutdown acceptance.

Each staged candidate restart performs full health in controller readiness, then fresh full health again in selector restoration. The minimal correction uses a bounded /version request only for controller reachability, explicitly reporting policy_proven=false. Selector restoration retains full fresh health and native readback, and final publication retains incarnation/config/revision proof. Transition and final selector restoration are not skipped. Regression coverage verifies readiness does not read full policy, malformed/unreachable controllers fail, and selector restoration failure still refuses restart success. Hosted qualification remains pending. Production unchanged.


## Controller-only readiness result — 38460094138

Source48ea7bd: fast push38460082173 PASS; actual PR38460094138 ordinary26 PASS/0 FAIL/0 SKIP, Provider11 PASS/0 FAIL/0 SKIP, complete packet1 PASS, scale1 FAIL. All owned resources removed. Chromium launch failure did not reproduce; previous SIGSEGV remains unexplained, not attributed to D-Bus, architecture or OOM without evidence.

Scale API VmHWM740376KiB, no readiness by45s, shutdown during unfinished startup exceeded8s. Live recovery15.5333s, selector5.4142s, Xray reconciliation31.9026s, DNS4.9564s. Deadline stack now lies in mandatory selector restoration's YAML policy parsing, confirming controller-only readiness no longer performs that expensive proof. Full startup/load/shutdown acceptance remains BLOCKED, not accepted based on the other38 scenarios.

Next correction reuses only the pure safe YAML parse for exactly identical current file bytes, with one bounded per-adapter entry and explicit invalidation on content/loader change. The checker does not mutate the parsed tree. Every health call still reads current file bytes, reads fresh native selectors/rules, recomputes all policy/readback checks and checks current reconcile receipt; no policy result, native proof or writer fence is cached. Direct static parser tests retain their normal uncached semantics. Source and hosted evidence will be recorded after implementation. Production remains unchanged and deployment is NOT RUN.


## Parser-cache fixture correction — 38462833271

Routine rejected one new test: the YAML flow sequence `rules: [MATCH,DIRECT]` means two scalars, not one comma-containing rule. Corrected the fixture to quote `"MATCH,DIRECT"`, preserving its expected single routing rule and all cache assertions. No application defect or assertion weakening. Native profiles NOT RUN in this run. Fresh protected backup `api-stability-20261013T202715Z`: schema integrityok/FK0; both archives fully read;434 installed backend files exactly matcha4a7a33. The archive is a fail-closed containment baseline, not a claimed stable control-plane or qualified Debian host rollback.


## Complete startup reaches DNS proof — 38463718242

Source0d4ab44: ordinary26 PASS, Provider11 PASS, original packet1 PASS; scale1 FAIL, all cleanup owned. Full startup34.0951s and observed graceful shutdown2.6185s satisfy unchanged timing budgets; API VmHWM782460KiB is below1.5GiB. Actual Xray reconcile succeeds, but DNS selective contract fails, so concurrency/steady waves remain NOT RUN and no live rollout is authorized by these partial results alone.

Source trace confirms synthetic manual DIRECT protected.test is the first non-reserved DIRECT domain selected by the real DNS nftset probe. The owned endpoint only answers direct.test/vpn.test; protected.test receives no address. The scale fixture now explicitly includes DIRECT direct.test before retained protected.test and asserts the actual production probe target selection is exactly the two owned endpoint names. This keeps the protected routing check and real DNS/nftset verification; no DNS production behavior or safety assertion is relaxed. Hosted verification pending.


## Sorted DNS target fixture — 38465658033

Ordinary26/Provider11/original packet1 PASS; scale refuses fixture before startup because its new target assertion exposes compiler ordering: lexical VPN scale-* domains precede vpn.test. Thus the previous DNS attribution was incomplete, not proof that adding DIRECT alone resolves it. The owned DNS fixture only answers vpn.test/direct.test and their subdomains. All synthetic scale VPN names now use scale-*.vpn.test (same99627 VPN domain count), and target assertion accepts only that owned VPN namespace and exact direct.test. No production DNS behavior changes; full DNS contract and workload remain pending.


## Preserve nonredundant scale rules — 38467607741

Ordinary26/Provider11/original packet1 PASS; scale1 FAIL before startup because the source validator correctly removes child suffixes already covered by parent vpn.test. This invalidated the intended99627-rule workload. Fixture uses99627 independent scale-*.vpn.test siblings without the parent suffix, still within the owned endpoint namespace. Exact count and DNS target assertions remain, preventing reduced-load false acceptance. Native startup/DNS/load for this fixture remains pending. No production change.


## Real sibling workload startup attribution — 38469657444

Source692373c: ordinary26/Provider11/original packet1 PASS; scale1 FAIL with99627 actual VPN suffixes and78 loaded synthetic identities. Startup45s deadline exceeded; API VmHWM793932KiB. Live recovery13.1988s, selector3.5603s, Xray profile reconciliation28.7028s, DNS phase1.8853s. Deadline stack is final Xray publication -> current Mihomo input fingerprint -> streaming file hashing; this single sample does not prove hashing dominates the28.7s phase. No guessed hash cache or fencing shortcut is introduced.

Observed process exited-SIGTERM after2.4189s but lacked verified application cleanup marker, so teardown remains FAIL; cancellation during synchronous unfinished bootstrap is not steady-state graceful shutdown acceptance. Detailed generation-stage timing diagnostics are prepared next. Full workload/native DNS success, API waves and live deployment remain unconfirmed. The earlier34.1s result is retained as partial observation with a DNS failure, not generalized to this corrected fixture.


## Hosted full PASS and controlled live gate — 38472054455

Actual4ffd686 run SUCCESS:1439 routine, qualified26+1, ordinary26, Provider11, packet/scale2 PASS; aggregate/source/cleanup eligible. Scale full bootstrap30.0161s,24 concurrent successful requests(max2.3812s), API VmHWM892128KiB, verified shutdown0.2137s. Prior failed results remain historical; one passing hosted receipt is not Debian host qualification.

User-authorized exact-feature backend/docs deployment4ffd686 completed and content verified436 files, protected guard/.env/native copy state preserved. Live API failed120s readiness and20s stop; maximumRSS751428KiB. Dependent units never resumed. Xray native count78, but8 UUID replacements and checkpointxray_applied establish unfinished generation, so further production changes stopped. Root cause/actual client impact not yet established. Native DIRECT/VPN probe/DNS and Rescue preservation observed after containment. See [controlled live report](LIVE_ACCEPTANCE.md). Production is now feature-source4ffd686 installed/APIheld, not unchangeda4; overall acceptanceBLOCKED.


## Read-only identity and checkpoint forensics — 2026-10-14

Protected before/after DB, configuration, checkpoint and native-user receipts establish the exact8-identity delta without exposing tokens, UUIDs or endpoints:

- Seven enabled public profiles retain their tokens. One inventory server is already marked missing in the pre-activation DB; one replacement is active. Seven subscription UUID changes are exactly deterministic uuid5 over the unchanged token and changed server ID. Seventy other native identities remain identical. One additional internal vpn-auto client is newly allocated for that replacement server; existing internal clients are retained.
- All seven committed public snapshots still contain their previous10-node sets (70 total), including the retired-server credentials. Native currently matches the checkpoint's candidate78 identities. This is an inventory-convergence candidate applied before public snapshot publication, not evidence of arbitrary rotation of all accounts. Stored old credentials no longer appear in the loaded native identity set; actual client disconnections or authentication attempts were not observed.
- Checkpoint phase is xray_applied. Active Mihomo bytes match the final candidate, although the durable phase has not advanced. Recorded selection revision164 versus current166, and changed Mihomo incarnation, prohibit automatic adoption/restore under the existing CAS contract. Xray incarnation matches when computed using the actual adapter formula. Native count and clean Core state do not establish generation consistency.
- A source defect is confirmed: transition digest was omitted from the durable checkpoint, while recovery attempted to read a validation record never persisted there. New checkpoints retain transition path/digest; recovery refuses missing/malformed transition proof and changed active config before native writes. Two component regressions assert unchanged full DB/files/checkpoint and no native mutation. A separate seven-profile regression records stable retained-server credentials, deterministic replacement credentials and unchanged verified snapshots before publication. Hosted execution pending.

### Readiness and shutdown attribution

Production evidence proves synchronous bootstrap did not finish before120s, checkpoint/native writes continued around the requested stop, and systemd reached its20s stop deadline. No live Python stack exists, so the dominant production function remains unproved. Do not equate the previous YAML/cache fixes with resolution of the entire regression.

Latest unchanged-source hosted repeat [38475000753](https://github.com/Coxf0rd/fwrouter/actions/runs/38475000753) is FAIL: original packet PASS; scale startup exceeds45s. Phase receipts show live recovery13.50s, selector3.59s, Xray reconciliation28.29s and DNS1.87s; peak sampled RSS785140KiB. This disproves repeatable startup acceptance despite earlier PASS38472054455. The test then issued a second SIGTERM while the first bounded shutdown was underway and replaced the primary startup failure with a shutdown assertion. Harness now sends at most one requested SIGTERM and retains both primary failure and independent shutdown metrics. Timing/memory assertions are unchanged.

The scale case is replaced with a realistic nonempty inventory:7 enabled profiles,9 owned native servers,70 public nodes,9 internal auto clients and2 fixed clients (81 identities),199k rules and99,627 UDP guards. It requires persisted-success inventory recovery, native bindings and verified snapshots, exact identity preservation,24 concurrent reads and real cleanup. Fixed test-only VLESS/Reality runs in the existing endpoint container with a loopback TLS1.3 decoy; no external handshake/DNS/egress or new stack. The known RFC7748 test vector is synthetic public fixture material; ephemeral TLS keys stay in the owned container. Native validation/performance of the expanded fixture remains NOT RUN until the new PR run.

Source-only validation:41 network-fixture contracts PASS,114 acceptance-tooling contracts PASS,50 statically inventoried acceptance nodes PASS, AST/diff/surface PASS. These checks do not run application/native services on the production host.

### Recovery plan — NOT EXECUTED

1. Keep API/workers/timers stopped; preserve protected preimage, DB and checkpoint. Repeat read-only identity/config/fence comparison immediately before any separately authorized recovery.
2. Do not restart into blind reconciliation or edit revision/incarnation/digest to make a stale checkpoint admissible. Do not restore the unsafe pre-fail-closed generator.
3. Qualify an isolated, fresh owned recovery transaction against current Core state using the checkpoint preimage's original78 identities and preserved subject/binding intent. Restore credentials exactly, not uuid4 regeneration. Verify whether the retired endpoint has a valid supported replacement mapping; absence of such proof is a blocker for declaring restored end-to-end client service.
4. Require native validation, mounted/generated/native identity parity, durable binding/snapshot publication, fresh CAS/owner proof, fail-closed and protected DIRECT preservation. Recovery of identity/config consistency and remote service reachability are separate gates.
5. Only after hosted nonempty startup/shutdown stability and user-authorized live recovery may API be activated in a bounded observation window; dependent units remain held until live readiness/health/identities/parity pass. Full live acceptance remains BLOCKED.

No restoration or production configuration change is authorized by this source checkpoint. PR8 remains DRAFT / BLOCKED pending hosted execution and demonstrated nonempty-profile performance. Existing protected raw evidence is retained outside Git.
