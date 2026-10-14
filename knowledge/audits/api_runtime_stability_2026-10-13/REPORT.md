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


## Nonempty native reproduction and narrow planner fix — 38478166408

[Actual PR run](https://github.com/Coxf0rd/fwrouter/actions/runs/38478166408), source68d1459:1439 routine PASS,26+1 qualified PASS,26 ordinary PASS,11 Provider PASS. Packet has2 failed functional scenarios plus a separate scale teardown error; no skips. Overall FAIL, not qualified acceptance.

The real nonempty-profile startup exceeds45s with sampled peak608004KiB. Deadline traceback is exactly prospective profile bindings -> Xray identity lookup -> effective-state builder -> default preflight -> Mihomo native rule/guard readback. The planner already holds routing/enforcement/bypass snapshots and then discards this per-client effective state by reloading the persisted subject and enriching it again. This confirms an avoidable per-client native-observation/source-artifact loop, unlike a guessed network or YAML cause. New private lookup option returns raw persisted identity only for this planner; ordinary lookup keeps fresh effective-state behavior. Final staged validation, source fingerprint, CAS, incarnation, native readback and publication checks remain unchanged. Two1/70-client component cases require one shared mode preflight, identical bindings/hand-off intent, unchanged DB and preserved ordinary lookup semantics. Actual production dominance remains unmeasured without its unavailable live stack; hosted reproduction is corroborating evidence, not a fabricated live trace.

The original packet test fails specifically when restarting the owned endpoint after the unavailable-proxy negative case. Added TLS material was created unconditionally on every Xray launch, so existing owned files correctly caused restart refusal503. TLS material now belongs to the endpoint role and is created once; the live TLS decoy persists while its Xray process stops/restarts. Safety checks, owned cleanup, port/network restrictions and native config validation stay intact.

Selection audit also found the new cases in profiled test_xray.py were not actually among its two narrow routine carve-outs or26 qualified nodes. Therefore the prior1439 PASS does NOT prove those new regressions. All new component cases are moved to the existing routine L3 test_xray_vpn_auto_lifecycle.py, reusing its established fake-native boundaries/helpers. No blanket profile bypass or broadened subprocess/network permission is added. The next run must explicitly execute all five parameterized new cases. Source-only41 fixture/114 acceptance/66 gate contracts PASS, source catalog50 PASS and surface/diff checks PASS; native qualification pending.

Fresh read-only DB/config comparison:78 committed active identities,78 native config identities,8 absent in each direction; all70 public snapshot identities remain in DB but7 are absent from native config. Eight active Xray overrides target missing/nonactive inventory entries. Existing automatic committed-projection recovery also requires valid complete bindings and public projection proof, so it must not be assumed admissible. Warning-only native logs expose no authentication receipt; absence of an error is not client-impact proof. No production mutation is performed.


## Explicit component DNS isolation — 38480400724

[Actual run](https://github.com/Coxf0rd/fwrouter/actions/runs/38480400724):1442 routine PASS/2 FAIL/0 SKIP; native profiles NOT RUN after the correctly failed routine gate. The two transition-proof cases and deterministic seven-profile inventory/snapshot case PASS under actual execution. The1/70 planner cases correctly refuse implicit `ip -json address show` through DNS status discovery. The component fixture now marks native DNS status explicitly unavailable, not mock-success, while actual subject/preflight/binding logic and all assertions remain. Native DNS proof remains required in the separate real packet/nonempty-profile scenario. Fast L0–L1 run38480395100 PASS does not execute these L3 cases. Production remains read-only/APIheld.


## Complete component proof, residual immutable-config CPU cost — 38481089296

[Actual run](https://github.com/Coxf0rd/fwrouter/actions/runs/38481089296), sourcee61802f:1444 routine PASS/0 FAIL/0 SKIP, all5 new L3 regressions individually PASS; qualified26+1, ordinary26, Provider11 and original real packet1 PASS. Nonempty scale1 FAIL at unchanged45s deadline, verified steady-state shutdown remains NOT RUN. API VmHWM741320KiB. All raw receipts retained privately.

The planner now completes in6.14s instead of remaining in per-client preflight at the deadline. Full trace: live recovery13.83s, selector3.65s, profile phase37.25s, DNS1.86s. Staging10.27s, changed Mihomo apply/selector restore10.93s, identical final apply0.043s, final publication3.99s. At45s the stack is full native rule normalization in restart selector restore. Completion later in captured logs does not establish on-time readiness or completed lifespan. All required native proof stays enabled.

Next narrow optimization extends the existing one-document config cache with configuration-only derived guard validation and normalized expected rules. Limits stay32MiB input plus250k expected rules; larger documents retain uncached streaming behavior. Loader/content/validator identity invalidate the entry. Native HTTP/rules/proxies/selectors, index/disabled/type/order comparisons, runtime incarnation and durable receipt proof remain freshly checked each call. The small additional retained expected-rule structure trades bounded memory for eliminating repeated immutable-config CPU work; RSS/startup benefit is NOT RUN until measured. Component coverage changes native rules under unchanged config, then config bytes, loader and validator, and checks uncached behavior over budget. Native TCP through the owned Reality endpoint is additionally required via actual request and endpoint source/count observation; listening/native validation alone is insufficient.

PR8 and production acceptance remain BLOCKED. No production activation/recovery is performed.


## Bounded config-analysis evidence and identical candidate render — 38484641870

[Actual run](https://github.com/Coxf0rd/fwrouter/actions/runs/38484641870), sourceb2b690b:1445 routine PASS/0 FAIL/0 SKIP, qualified26+1/ordinary26/Provider11/original packet1 PASS; nonempty scale1 FAIL at unchanged45s deadline. RSS HWM819432KiB. Phase sum is approximately47s: live recovery11.90s, selector2.33s, profiles30.96s, DNS1.85s; profile phase improved from37.25s. Final native proof is still fresh (deadline stack in native JSON decode), not bypassed. New Reality TCP probe and steady waves remain NOT RUN because startup fails first.

The same trace confirms transition/final apply digests are identical: final apply is a verified unchanged path0.036s, but the identical candidate is rendered twice (2.20/2.26s). Prepared optimization atomically copies the already validated transition bytes only when handoff inputs, full Mihomo input fingerprint, all bounded fallback-base file hashes and renderer identity are unchanged. Both local and native validation remain; unknown context, distinct handoffs or changed inputs retain separate rendering. Before/after digest and post-validation context checks reject mutation. Three L3 cases require both validators even when no immutable validator image identity is available.

Config-analysis retention is additionally bounded by expanded expected-rule UTF-8 size, not just32MiB input/250k row count; YAML aliases or non-string rules retain streaming fallback. Regression covers a small aliased document exceeding the expanded budget. New changes are source prepared / hosted NOT RUN until the next run.

### Identity origin clarified by read-only source/DB comparison

Retired and replacement entries have the same display name, identical native transport/endpoint/credentials fields and exactly the same source-membership set. Only runtime name/server-ID/raw-identity digest/topology/name decorators differ. New inventory first_seen is2026-10-13 12:01:55; old missing_since12:02:30, before the21:54:45 activation. Neither current global Auto nor fixed target is that entry. Parser stores raw-entry identity and derives runtime/server names from its digest; changed raw identity rekeys per-server public credentials when startup finally converges it. Exact changed raw source bytes are not recovered yet: do not invent a specific query-order/label change or blame the new PR8 source. No parser/identity namespace contract is silently changed.

Protected preimage config has both required VPN groups, all owned groups end in REJECT without DIRECT, and paired guards in top/transparent/full-VPN contours; this is pure configuration evidence, not native qualification or permission to restore. Current original generation source fingerprint matches checkpoint, but stale revision/incarnation still forbid direct restore. Original78 credentials remain in DB/preimage. A fresh owned recovery must qualify equivalence/source ownership and restore consistent identity/binding/public state, preserve current unrelated Core intent and fail-closed, and handle the next explicit subscription refresh without repeating an unannounced rekey. Existing marker must not be hand-edited or blindly discarded. Production recovery and complete Live Acceptance remain BLOCKED/NOT RUN.


## Full upstream profile delta and next native normalization checkpoint

Historical protected DBs081308Z/111322Z/114358Z retain original raw-entry metadata. Current metadata retains the replacement. Both server IDs exactly equal `sub:` plus SHA256 of their original canonical JSON profile bytes. This is NOT a URI slash/query-order difference. The only top-level change is outbounds:10→13 entries, unchanged first7 VLESS entries,3 new VLESS entries inserted before the original freedom/blackhole/dns service entries. All other profile fields are equal. Thus normalized primary transport equality did not establish full logical topology equality; the profile's member inventory really changed. Previous metadata-only hypotheses are superseded, not presented as facts.

Parser `_servers_from_json_profile` hashes the entire canonical profile; `logical_topology.sync_logical_topology` uses that server ID as the logical profile key. Startup converges the previously persisted new profile, deriving7 public credentials plus1 internal auto credential. No new PR8 UUID algorithm caused this. A permanent no-rekey policy across logical-profile member changes requires a deliberate compatibility/identity mapping; no global identity contract is silently changed in this performance fix. A recovery must preserve original78 credentials while choosing either an explicitly frozen last-good7-member profile or a qualified10-member migration retaining client identity. Primary-endpoint-only aliasing is insufficient. Current global Auto/fixed target remains unrelated to this pair.

[Actual328549a run38489691937](https://github.com/Coxf0rd/fwrouter/actions/runs/38489691937):1450 routine PASS, all candidate-copy/context/corruption and YAML-expanded-budget cases PASS; qualified26+1, ordinary26, Provider11 and original packet1 PASS. Scale1 FAIL at45s; HWM791620KiB. Candidate render is now once with both validators; staging8.16s versus10.19s. Profile phase29.53s, live recovery12.19s, selector2.45s and DNS1.91s remain just above deadline. Reality TCP/steady-state acceptance NOT RUN.

Next narrow CPU change recognizes only the exact simple UDP/domain guard grammar used by the generated99,627 guards. Equivalent canonical/CamelCase condition names and native comma/AND separators map to the same canonical tuple; all other compound/CIDR rules retain existing generic validation. CIDR substitution skips inputs that cannot contain the exact CIDR pattern. Native data remains fresh, ordered/disabled/index/type/target checks unchanged. Regression requires valid equivalent spellings and rejects an extra condition. Measured benefit pending; no budgets are relaxed. Source/fencing/native publication safety remain intact. Production is read-only/APIheld; PR8 remains DRAFT/BLOCKED.


## Nonempty startup within budget; auxiliary fixture identity isolated — 38491844089

[Actual d4ba217 PR run](https://github.com/Coxf0rd/fwrouter/actions/runs/38491844089):1451 routine PASS/0 FAIL/0 SKIP; qualified26+1, ordinary26, Provider11 and original packet1 PASS. Nonempty scale1 FAIL after successful36.7484s startup. API HWM809980KiB; final cleanup observed/graceful1.6666s. Profile phase21.59s, Core live recovery8.96s, selector1.73s, DNS1.50s. These are startup/cleanup observations, not completed scale acceptance: real Reality TCP and steady concurrent HTTP waves remain NOT RUN at this source.

The next assertion finds82 applied bindings versus the intended81. The shared Xray fixture also contains one acceptance-provider-upstream client on a separate inbound; replacing vless-ws clients did not remove it. Startup inventory converges that auxiliary client, while loaded_identities and seeding count covered the managed inbound. The dedicated scale variant now removes only that known auxiliary inbound and its matching inbound-specific rule, preserves API/managed configuration, and refuses unknown client-bearing inbounds. Other scenarios retain their shared upstream. Exact applied identity-set equality is added; neither count nor identity assertions are relaxed. Two pure fixture regressions verify exact ownership, no input mutation, API preservation and refusal of unexpected clients. Source-only tooling236 PASS, surface/diff PASS; next hosted execution pending.

Read-only recovery clarification: current-projection fallback first checks binding/mode/public-snapshot parity, then bindings-artifact parity and mounted/native configuration. The observed8-identity mismatch guarantees refusal at its later config-identity equality check if earlier gates pass. Its actual first refusal is NOT OBSERVED; no recovery execution or native mutation is performed. Permanent credential preservation across the changed full-profile identity still requires authoritative continuity mapping, not primary endpoint/display-name aliasing. Production remains held, Live Acceptance BLOCKED, PR8 DRAFT.


## Repeated bounded startup; binding origin still unresolved — 38534480181

[Actual c49aed0 PR run](https://github.com/Coxf0rd/fwrouter/actions/runs/38534480181) remains FAIL: native qualified26+1, ordinary26, Provider11 and original packet1 PASS; nonempty scale1 FAIL with82 versus81 bindings. Startup37.3019s, HWM806064KiB, observed graceful cleanup1.7171s. The two consecutive starts are within45s, but Reality TCP and steady HTTP waves remain NOT RUN. Removing the known auxiliary inbound did NOT resolve the extra binding; the prior claim that it fully explained the count is superseded. No product fix is guessed from count alone. Prepared diagnostics record missing/unexpected/duplicate identity counts, native identity preservation and at most4 sanitized subject-origin summaries before the assertion; no UUID/email/token/config content is exported.

The existing seven-profile identity regression now follows real parser normalization for a synthetic7→10-member JSON profile, verifies exact canonical hash/server ID change despite unchanged name/primary endpoint, and then checks deterministic credential replacement/retained credentials/unchanged published snapshots. This is evidence of the existing contract, not a new continuity policy. Hosted execution pending at this checkpoint.

Source design review confirms no current durable old→new identity mapping. Subscription snapshots are published export state, not desired-client authority; subject_server_overrides selects routing targets and cannot serve as a credential migration registry. A one-time snapshot rewrite would be undone by the next desired-client reconciliation. Safe permanent restoration requires an explicit scoped persistent continuity/migration contract consumed by both desired-client generation and verified export, including provenance, uniqueness, lifecycle and subsequent refresh semantics. Do not alias profiles by name/primary endpoint or bypass recovery fences. This is an architectural recovery gate, not permission to alter production. API/dependents remain held and Live Acceptance/PR merge readiness remain BLOCKED.


## Startup variance and earlier binding diagnostics — 38536795843

[Actual92de4c9 PR run](https://github.com/Coxf0rd/fwrouter/actions/runs/38536795843):1451 routine PASS, including the real-parser7→10-member/seven-token identity case; qualified26+1, ordinary26, Provider11 and original packet1 PASS. Scale1 FAIL at45.6359s; HWM793344KiB. Late binding-origin diagnostics do not execute because readiness fails first. Prior36.75/37.30s starts are therefore not reproducible qualification. Phase trace: live recovery11.27s, selector2.04s, profiles27.10s, DNS1.86s. The deadline stack is owned dnsmasq readiness inside actual startup reconcile, after the profile publication call returned. This is not evidence of a new DNS architecture defect or justification to extend the45s budget. Steady health/Reality TCP remain NOT RUN.

New diagnostic hook observes prospective bindings immediately after the actual planner returns, before staging/apply/startup deadline. It compares against the existing managed-inbound client input and emits only bounded counts/known role classes/boolean origin flags. It does not modify returned bindings, native state, identity derivation, writer ownership or validation. Tooling237 PASS includes duplicate/missing/unexpected counting and rejection of arbitrary metadata/credential export. Source/native execution pending.

Fresh read-only file comparison: surviving transition/final candidates differ, active Mihomo equals final and final matches the checkpoint digest. Legacy transition bytes are not authenticated by that checkpoint. The observed boundary is xray_applied ledger versus final active bytes, not verified final native publication. Current restore fences remain inadmissible.

### Proposed durable recovery contract — NOT IMPLEMENTED

The minimal durable design is an explicit scoped identity migration record in existing SQLite, covering `(subscription token, old canonical server ID, new canonical server ID, retained UUID/email, provenance digest, revision)` and the separate internal vpn-auto identity scope. Both desired-client generation and verified snapshot/export must resolve it consistently; no name/host matching or treating an export snapshot as desired intent. Require one-to-one source ownership, exact protected DB/preimage credentials, no duplicate UUID/email, and native/CAS/source proof before committing/publication. An unmapped subsequent profile-key change must preserve last-good bindings and defer publication instead of silently rotating identities. It must not imply remote DOWN or DIRECT permission.

Acceptance requires original78 restored exactly, unrelated70 unchanged,7 public snapshot credentials/native parity, internal identity continuity, repeat restart/refresh, concurrent CAS refusal, crash-safe publication and no stale success. Mapping state must be included in backup/fingerprint/rollback compatibility; older consumers that ignore the mapping are not qualified rollback targets. This changes persistent identity and compatibility semantics and remains a distinct architectural decision before implementation, not a hidden one-time DB/snapshot repair. No production recovery/activation is performed. PR8 remains DRAFT/BLOCKED.


## Exact extra-identity cause: omitted internal virtual target — 38540649011

[Actual499d23f run](https://github.com/Coxf0rd/fwrouter/actions/runs/38540649011) still FAIL: native qualified26+1/ordinary26/Provider11/original packet1 PASS, scale1 FAIL after43.265s startup; API HWM803828KiB, observed graceful cleanup2.3185s. Early planner diagnostics prove82 unique bindings, no duplicate/missing expected identity, and one extra new internal vpn-auto identity with no existing subject before generation. Post-start it is an active explicit VLESS subject from xray_adapter and native identity set has grown. This is not auxiliary-inbound or cross-test state evidence.

Source `_vpn_auto_servers_for_xray_subscription()` always prepends the virtual vpn-auto target to real eligible servers. The fixture seeded9 internal clients only for its9 real endpoints, omitting that legitimate internal virtual target; reconciliation correctly allocates it. Corrected fixture seeds the actual authoritative internal list, requires its exact set to be virtual+9 owned real targets, and preloads all10 internal clients. Baseline becomes70 public+10 internal+2 fixed=82 before startup. Exact binding set/count and native-before/after equality remain mandatory, so this is not accepting an unexpected identity after apply. Public nodes stay70, real endpoints9; no production identity derivation or routing logic changes. Hosted82-before/after acceptance pending.

237 retained source-only tooling contracts previously PASS; the native assertion itself supplies meaningful identity-preservation coverage. No additional constant-mirroring test is retained. Surface/AST/diff PASS. Stable UUID migration across changed profile IDs remains NOT IMPLEMENTED/architectural recovery gate. Production remains unchanged in this read-only phase.


## Planner parity confirmed; redundant diagnostic artifact export removed — 38543007189

[Actual312567e run](https://github.com/Coxf0rd/fwrouter/actions/runs/38543007189) remains FAIL at45.2786s startup, HWM792340KiB. Early planner receipt now proves82 rows/82 unique, zero missing/unexpected/duplicates. This confirms the authoritative82-client preseed resolves the new internal-client planning delta, but does not establish completed native-before/after or steady acceptance. The deadline stack is actual fresh effective-rules JSON decode in DNS nftset readiness; no readiness success is inferred.

Source review finds observer `_packet_dns_effective_artifact_observation` decoded the large artifact independently and then invoked `get_manual_rules_texts()` just to obtain its writer path. That read-model decodes the same artifact again and loads all text exports before the actual production fresh reader runs. The observer now uses lightweight `get_rules_state()` for the path. Independent uncached/cache parity, real fresh DNS read, DNS/nftset probes, exception propagation and all timing assertions remain unchanged. Tooling237 PASS includes a negative assertion forbidding diagnostic export-read-model invocation and the original cached-None versus independently present-artifact regression. Performance benefit awaits hosted measurement; this is removal of known measurement overhead, not a speculative product or DNS change.


## Complete82-identity publication and HEAD observation defect — 38545654547

[Actualf8c51e0 run](https://github.com/Coxf0rd/fwrouter/actions/runs/38545654547) remains FAIL. Native82-before/after exactly unchanged, bindings82 unique with zero missing/unexpected, all7 profiles runtime_verified, generated policy99,627 guards/protected DIRECT confirmed; API HWM804324KiB and observed graceful cleanup2.3183s. Steady24-request waves remain NOT RUN. Native Reality delay returns HTTP200/3ms but observation delta0 and no VPN peer proof, so transport acceptance is not declared.

Pinned [Mihomo v1.19.31 URLTest source](https://github.com/MetaCubeX/mihomo/blob/v1.19.31/adapter/adapter.go#L218) sends HEAD. The owned endpoint tracked /probe only for GET and returned404 on HEAD. Add HEAD /probe only on the service VIP/port, record the actual peer, return200 with zero body; control endpoint still404 and /generate_204 still contributes no probe counters. Existing source-only handler contract now checks correct count/peer/HEAD headers and refusal on the control port. Native verification pending; no upstream HTTP200 alone is accepted as proof.

Timing audit additionally finds readiness polling can return a200 after its deadline: recorded startup45.1579s had been allowed to proceed. The request timeout is now clipped to remaining deadline and a late200 is rejected; the scale test also requires measured wall time strictly below45s. Thus this run supplies completed identity/publication observations but NOT on-time startup acceptance. No timing budget is raised. Tooling237 PASS, AST/surface/diff PASS; next hosted qualification pending.


## First strict full nonempty hosted PASS — 38548698209

[Actualca1cd8a PR run](https://github.com/Coxf0rd/fwrouter/actions/runs/38548698209) SUCCESS:1451 routine/0 FAIL/0 SKIP; qualified26+1, ordinary26, Provider11, original packet plus scale2 PASS. Source/plan/aggregate/cleanup PASS. Native82 identities remain exactly unchanged,82 bindings match, all7 snapshots verified. Native Reality HEAD receives200/5ms; owned endpoint records exactly one request from VPN egress source. 24 concurrent API reads pass with fresh native rule/wiring/receipt proof. Startup44.8415s, process-lifetime HWM908944KiB (~0.867GiB), readiness HWM791892KiB, observed graceful shutdown0.2639s.

This is the first strict full nonempty PASS, not reproducibility qualification: startup margin is only0.1585s and prior snapshots failed. A current-head repeat is required before calling startup stable. The scale case qualifies bounded API/native/config behavior, not canonical remote availability of every synthetic server: its recorded selector problem_code active_auto_server_invalid is permitted in this fixture with no canonical measured server-health rows; native selected owned target/proxy inventory parity is checked. It must not be presented as healthy remote-provider inventory. Separate Provider/native cohort passed11 cases.

Production remains installed4ffd686 with API/dependents held; no live repair or deployment in this phase. Original78 restoration/durable profile continuity still NOT IMPLEMENTED, safe fresh recovery still NOT RUN. Live Acceptance and PR merge readiness remain BLOCKED pending recovery-contract decision and repeat performance evidence. L6/L7/host-systemd qualification NOT RUN.


## Repeated strict hosted acceptance and architectural recovery gate — 38551736466

[Actual47d64e2 PR run](https://github.com/Coxf0rd/fwrouter/actions/runs/38551736466) SUCCESS on a fresh runner with no backend/test/workflow diff against successfulca1cd8a:1451 routine,26+1 qualified native,26 ordinary,11 Provider,2 packet/scale PASS,0 FAIL/SKIP. Startup28.1711s, process HWM947256KiB (~0.903GiB),24 HTTP200 reads, max1.5216s/health0.0041s, observed graceful cleanup0.2136s.82 native identities unchanged,82 matching bindings,7 verified snapshots, actual Reality HEAD2ms with exactly one owned VPN-source endpoint observation. Together38548698209/38551736466 establish two full scoped PASS; timing varies28.17–44.84s, so no worst-case margin or long soak is claimed.

Production remains read-only/APIheld, installed4ffd686; original78 credentials/native/public state remain unresolved. No original identities are rewritten and no resume/reconcile is attempted. Recovery is blocked by stale legacy fences and missing valid committed binding/public/native projection. Source transition-proof defect is fixed and tested, but it cannot authorize that legacy marker.

**Status: hosted API/native scale qualification PASS (two runs); Production Live Acceptance BLOCKED; PR8 remains DRAFT pending the identity-continuity/recovery contract decision.** The canonical-content-derived server key is mutable when membership grows. Restoring original credentials permanently across that key change requires choosing a durable identity migration model with explicit old/new source proof and retained credentials, or deliberately freezing the last-good profile. Existing snapshots are not durable identity intent, and routing overrides must not be repurposed as credentials. The proposed scoped migration and compatibility/refresh/CAS/native/rollback criteria above are concrete but NOT IMPLEMENTED. User decision on this persistent identity contract precedes that implementation; no production authorization is implied.

A documentation-only closing checkpoint can be kept locally until the next substantive source push; no third full hosted rerun is needed just to restate these unchanged-code receipts. Protected artifacts/JUnit/logs remain under /root/fwrouter-recovery/pr8-38548698209 and pr8-38551736466; no private credential data is included in Git.


## Approved durable credential continuity — implementation checkpoint (2026-10-14)

The user approved durable migration mapping in PR #8. Implementation is in progress and is **NOT QUALIFIED**: local source/test drafts have not yet run targeted hosted acceptance. Production remains unchanged in this phase; no held API/workers/timers are started. PR #8 remains Draft. The existing local documentation commit `6285320` and private/raw evidence are preserved.

Persistent credential intent is separated from content-addressed provider inventory. The additive schema-25 draft records logical node identities, historical server aliases, scoped public/internal credentials and witnessed transitions. Public subscription credentials and the internal VPN-auto identity must both retain their original UUID/email. Automatic continuity requires unambiguous source ownership and strict ordered retention of canonical outbound witnesses; display names or primary endpoints are not sufficient evidence. Policy/content edits need an explicit reviewed manifest. Credential rotation and revocation are explicit, fenced operations rather than consequences of refresh.

The migration operator path is being integrated before ordinary generation. Protected manifest admission defaults to dry-run, with a plan-bound explicit apply intended to change DB intent only. It does not publish snapshots or resume the old checkpoint. Fresh generation supersession must retain the byte-exact old marker until a newly validated candidate and durable replacement checkpoint exist. Archive plus atomic marker replacement must leave no marker-absence window. Source, selection, incarnation and active/mounted hashes require fresh revalidation; old checkpoint fences are not rewritten.

Recovery must not describe the present mixed native identity set as a healthy rollback target: restoring files does not establish identity/native/public parity. Inconsistent recovery must remain BLOCKED with evidence retained. Isolated synthetic 78-identity acceptance, native readback, refresh/restart/recovery/rollback, and privacy checks remain pending. Real production credentials are not CI fixtures.

Startup remains an open performance risk: the measured 44.8415 s run has only 0.1585 s margin under the unchanged 45 s gate, despite a 28.1711 s repeat. Independent read-only review attributes the largest stage to nonempty profile convergence (26.6608/17.1478 s), separate from kernel cold repair (10.861/6.4344 s). A confirmed redundant persisted-subject lookup in prospective binding planning is being removed; its actual benefit and adequate margin are not yet measured. No native validation/readback/fencing or cold repair gate is removed.

Validation order: coherent source/test package → safe static/pure SQLite contracts → targeted GitHub-hosted migration/native/scale checks → one final full required CI run. No microfix-by-microfix full CI, production rollout, merge, L6/L7 or unrelated milestone work is authorized by this checkpoint.

### Coherent source package / targeted qualification entry

The implementation package now includes offline schema-24→25 upgrade with explicit offline confirmation, private verified SQLite backup, protected manifest preview/apply, stable public/internal credentials, credential retirement history, witnessed transition history, conservative migration-required inventory markers, fresh checkpoint supersession and proof-bound alias recovery. Ordinary generation cannot bypass a checkpoint linked to migration intent. Checkpoint files are written privately with file/directory fsync and atomic replacement; the old marker is retained byte-exact in a protected archive. Explicit revocation omits credentials from desired/public output without permitting reuse. Old schema-24 code is not a compatible schema-25 rollback target; no automatic downgrade/old generator restore is provided.

Safe local validation after source freeze: changed AST/JSON parse PASS; manifest `2026.10.14.1` (170 test files) PASS; 251 pure tooling/SQLite contracts PASS (12.422 s); diff/surface guards PASS. Local changed-source Ruff F821 is NOT RUN because the pinned Ruff executable is unavailable; the hosted preflight remains mandatory. No backend pytest or native/application test runs on the production host.

Hosted catalog now contains38 functional cases (27 ordinary +11 Provider), plus2 packet/scale cases. Previous37-case/39-total evidence remains historical and is not represented as proof of the new case. The new native migration test must prove a78-identity baseline, protected preview/apply, an actual persisted PREPARED failure boundary, graceful restart, stale-marker refusal, fresh supersession, refresh and native-loaded parity. This is controlled failure-boundary/restart qualification, not SIGKILL/L7 or Debian host acceptance.

A trusted branch-only diagnostic marker intentionally stops the stable required PR job before full tests with an explicit failed/NOT-RUN message. The diagnostic push runs exact migration/schema nodes, the native migration case and two independent cold scale runs. It cannot qualify merge readiness. The final normal push must execute the full required gate once after targeted success. Both the unchanged45 s hard gate and the desired40 s engineering margin are recorded; a narrow passing startup is not declared risk-free. Production remains unchanged and PR8 Draft.

### Targeted run38559617367 — pre-test infrastructure refusal

Source8719d71 was pushed with the coherent migration package (local6285320 was preserved/pushed). Run38559617367 failed before test execution because the reused historical PhaseD workflow expected ancestor6bdf37e, which is unavailable in a fresh checkout after the CI milestone squash. Native/migration/startup gates were SKIPPED/NOT RUN, not PASS. The stable required PR job38559623104 intentionally refused diagnostic-mode full CI before installation/tests; that result is not a product regression or acceptance.

The diagnostic preflight and plan now use confirmed immutable main1b302e91b3cd652cd4aab4f45bbdd88c088e5786 only for the new identity stage; historical stages retain their own base. Source/plan/base validation remains mandatory. Safe checks after this infrastructure correction:252 tooling/SQLite contracts PASS, manifest/AST/JSON/diff PASS. Hosted targeted rerun is pending; PR8 remains Draft and production unchanged.

### Legacy admission / static qualification correction

Run38560147367 at2636eba reached the immutable main base but failed F821 at one missing local `credentials_for_scope` import. No migration/native/startup tests ran; downstream SKIPPED remains NOT RUN. The import was corrected in the same group as legacy-target qualification and revocation coverage. Exact hash-pinned Ruff0.17.0 was staged under `/tmp` only, without modifying the production/project environment. Final local batch: F82168 changed Python files PASS; AST/JSON PASS; manifest170 files PASS;252 pure tooling/SQLite contracts PASS (12.34 s).

Protected read-only admission confirms one exact canonical profile growth7→10, seven public and one internal affected credential,70 unchanged, and all78 active DB identities equal the protected pre-activation set. A0600 private manifest and admission note remain outsideGit; no CLI preview/apply, schema upgrade, native write or production activation occurred. Current schema24 requires explicit offline25 upgrade. Legacy target rows without provenance can now be admitted by full canonical/raw hash, parser format/server ID and exact normalized parser projection; mismatches refuse. Verified provenance is installed in the same intent transaction, with dry-run rollback. This removes the unnecessary external Provider-refresh prerequisite.

The hosted case additionally checks explicit revocation: after proving78 preserved identities, one synthetic requested tombstone must leave exactly77, deny public credential export, survive refresh/restart and never reallocate its retired UUID. This intentional revocation is not silent loss of an original production identity. All hosted results for the corrected package are pending. Production remains unchanged, PR8 Draft, startup margin unqualified.

### Checkpoint I/O / diagnostic evidence correction

Run38561318039 at7ee2c57 passed static/bootstrap/infra and subscriptionL1 selection, then the exact migrationL3 step failed. Its new L3 output directory was not retained by the upload step, so named node results cannot be reconstructed; downstream native/startup remain NOT RUN. The corrected workflow now uploads an explicit sanitized L1/L3 artifact allowlist from the producing job, prints a bounded redacted failure tail, and executes the small adjacent XrayL3 cohort after exact nodes pass. No missing result is counted PASS.

Fast run38561317730 preserved concrete evidence:15 PASS /2 FAIL in `test_subscription_refresh_outcomes.py`, both staged callback/rollback ownership cases. Common cause was the new private checkpoint writer's writable `fdopen` event, rejected by the fail-closed test bootstrap. Writers now use bounded `os.write` on an already audited, exclusively acquired0600 descriptor; no bootstrap permission was weakened. Additional review corrected double-close ownership to prevent closing a descriptor reused by a concurrent thread. File/directory fsync, exact archive and atomic replacement are preserved. Eight parameter cases now cover write/fsync/replace/link failure cleanup and descriptor reuse/single-close. These component cases still require hosted execution.

The next coherent target also includes the two observed callback failures and the four-file XrayL3 subset (reconcile/recovery/gateway/lifecycle). Local pinned F82169 changed Python files, AST/JSON, manifest170 files, diff check and252 pure contracts PASS after the grouped corrections. Native migration/recovery and cold startup measurement remain pending, as does the one final full required CI. No production or unsafe rollback action occurred.
