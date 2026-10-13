# Pre-deployment Rescue isolation and safe recovery

## Current checkpoint

Source implementation is complete on `fix/predeploy-rescue-isolation-safe-rollback`, based on accepted main `ef46147`. CI milestone remains CLOSED. Production deployment and live acceptance are NOT RUN. Hosted baseline qualification is recorded below. Final HEAD readiness is determined by the actual required check on PR #6; this report never substitutes for that check.

External Rescue: **PASS — user-confirmed manual external verification**. The user reports the Mac Rescue VPN fully works. This supersedes the earlier missing external proof; no Codex-observed handshake, commands or timestamps are claimed. Rescue configuration is unchanged.

## Confirmed causes and boundaries

`services/network_contract.py::lan_interface_allowed` previously admitted `rescue0` because the empty allowlist and default deny prefixes did not classify the reserved interface. `dnsmasq::_discover_router_dns_bindings` feeds address selection, local LAN hostname generation, DHCP DNS advertisement and DNS capture. Its sorted addresses caused the Rescue VIP to precede the actual LAN address. Production preflight observed the VIP in local hostname and forced DHCP DNS files. DHCP exposure and DNS leak incidents were **not established**.

The fix defines one exact reserved role map and rejects that role before configurable LAN discovery filters. Existing custom physical, bridge, VLAN and explicit tunnel names retain previous filtering semantics. Unknown names are not assumed to have a reliable automatically inferred role; deployments requiring narrower discovery use the existing explicit allowlist. No new global tunnel exclusion is introduced. Protected CIDR discovery is deliberately preserved: protecting management destinations is not LAN admission.

DNS discovery propagates to `dataplane_nft_sets::_resolve_lan_ingress_interfaces`. The network API contract reports the reserved role. There is no new persistent role table, DB migration, competing writer or runtime ownership mechanism. Core/CAS/fencing/native proof, explicit/protected DIRECT and identities remain intact.

## Recovery strategy

The installed snapshot `3d285c3` / identical-code `c1e00b5` is **not an admissible rollback target**. Its nft classify generator sends VPN-default traffic to DIRECT when selective VPN policy is unready; the current generator drops it. Older Mihomo generators also lack mandatory UDP guards and use DIRECT in empty selectors. Restoring files and restarting old reconciliation can recreate unsafe native policy even if a safe config was initially retained.

Recovery therefore uses an immutable, content-verified backend artifact from a safety-qualified checkout. This is repair to known-safe source, not permission to downgrade to the archived insecure implementation. The archive of the old installation remains useful forensic evidence, not a live restart target.

`installer/safe_recovery.py` prepares/verifies a tracked-source artifact and rehearses the standard backend-only installer against private temporary targets. An operator-trusted hosted receipt binds the source and content digest to required safety gates. Such a receipt is an evidence admission input, not independent cryptographic verification of GitHub or a substitute for actual native execution. Synthetic receipts in component tests provide no native acceptance.

### Compatibility matrix

| Target | Eligibility |
|---|---|
| Archived installed 3d285c3/c1e00b5 | REJECT: mandatory VPN may become DIRECT |
| ef46147 | Existing native fail-closed acceptance; Rescue classification still unsafe |
| New safety checkout with matching hosted proof | Candidate for backend recovery; production activation NOT RUN |
| Different source, missing proof, tampered archive | Tool rejects |
| Incompatible database/schema | Operational admission rejects; no generic downgrade support |

Database/schema compatibility is a separate admission requirement, not inferred from an archive hash. Current schema 24 has no migration delta in this scope. Preserve current DB intent, current identities and native configurations. A historical DB backup must not overwrite newer state merely because code restoration is requested.

## Host readiness protection and activation procedure

Live `traffic-collect-api.sh` has an additional readiness guard absent from repository host source. Host installation would overwrite it. Keep deployment/recovery limited to backend/docs; no host, native-image, unit or networking installation. Installer deploy mode does not restart services, but it still runs backend bootstrap. A later API restart runs the existing boot preflight, which includes host sysctl handling; it is not a pure HTTP restart.

Future activation requires a fresh protected baseline and host-helper hashes, an admitted recovery artifact, quiescence of mutating jobs and a separately approved deployment window. Restore code with the standard installer, preserve DB/.env/native state, compare installed source and guard hashes, then activate through existing Core startup/reconcile and require desired/applied/native readback before acceptance. A copy exit code alone never proves recovery. If native validation, recovery fencing or readback fails, do not publish success or restart the unsafe archive; preserve management/native state and use independent Rescue for the approved recovery procedure.

The staging tool deliberately cannot operate on `/` or control services. It does not solve atomic live application activation, host crashes or reboot. Those remain separately qualified host/release operations. No production rollback is claimed from component tests.

## Test inventory and evidence

- Existing network-contract tests: reserved Rescue override rejection and compatible LAN names.
- DNS reconciliation: actual discovery with synthetic link observations, deterministic multiple LANs, DHCP/local hosts/capture candidates, Rescue-only no-LAN handling.
- Existing real packet profile: owned dummy Rescue interface, actual ip readback and production DNS discovery/generation with native dnsmasq and cleanup.
- Recovery component tests: tracked content, incompatible/unqualified proof, tampering/traversal/symlinks, real isolated installer, DB/.env/host sentinel preservation and interrupted/repeated staging.
- Existing mandatory-VPN/empty-pool/UDP/protected DIRECT/Emergency Direct and application rollback/fencing gates remain mandatory where selected.

Hosted baseline [38373770914](https://github.com/Coxf0rd/fwrouter/actions/runs/38373770914) on `1499494`: **1392 routine PASS, 26+1 qualified native PASS, ordinary26 / Provider11 / packet1 PASS; 0 FAIL / 0 SKIP**. All seven historical regressions PASS. Strict aggregate eligible with no blocked reasons/missing levels, owned cleanup and actual full-source recovery artifact staging PASS. See HOSTED_CHECKPOINT.json. The additional real installer-exit42/restage scenario and obsolete-helper cleanup are later source additions requiring the final PR check; their execution is not inferred from this older run. L6/L7 and Debian host/reboot qualification are NOT RUN. DHCP lease exposure experiments and production routing experiments are NOT RUN.

## Fresh GO/NO-GO

Production remains **BLOCKED / NOT RUN** until the user merges the safety PR, the resulting main SHA passes actual CI, recovery evidence is admitted for that exact content and a separate deployment GO is issued. A source/PR PASS does not fix the installed production. The next deployment target is the new merged SHA, not automatically ef46147. A product regression requiring a different rollback version needs a separately safety-qualified target; recovery-to-same-safe-build does not promise to undo every application defect.

## End-to-end ownership review

| Path | Owner / result |
|---|---|
| Interface admission / role | Shared network contract; exact Rescue role wins over LAN configuration |
| LAN VIP / hostname | dnsmasq discovery and `_build_local_hosts_dnsmasq_text`; no first-interface fallback when no LAN |
| DHCP DNS | `reconcile_dnsmasq_rules`; generated forced DNS list uses filtered LAN discovery |
| DHCP binding / DNS listening | Existing host dnsmasq configuration, not a separate FWRouter discovery writer; observed DHCP binding enp2s0; no production listener changes |
| DNS capture / native ingress | Filtered bindings feed capture and nft LAN ingress resolution |
| Network API / persistence | Existing environment-owned filter settings and network contract manifest; no new role DB/API writer |
| Protected management / firewall | Protected network discovery unchanged; Rescue DIRECT safeguards preserved |
| Reconcile / restart | Every discovery repeats the same reserved-role rule; owned packet dummy must be removed on teardown |
| Installed backend provenance | Fresh read-only comparison matches 248 Python paths in archived snapshot, including startup_trace; prior 247 count is common target paths excluding that removed helper |
| Installed host provenance | Mixed host content: readiness helper differs from source; no exact whole-host commit label is claimed |

`startup_trace.py` is now an explicit obsolete installer path because the new source removed it and has no remaining imports. Unknown stale executable modules are rejected by recovery staging, rather than silently certified. Host helper content hashes are recorded in SOURCE_BASELINE.json without secret contents.

## Evidence depth / remaining qualification

| Scenario | Concrete coverage | Boundary |
|---|---|---|
| Restore / repeat restore | `test_prepare_verify_and_repeat_isolated_stage_preserve_state_and_host_sentinels` | Real installer, files/state preservation; no live restart |
| Reject incompatible proof / tampering | recovery admission tests | Before any installer apply |
| Partial file copy and retry | `test_real_installer_failure_after_copy_reports_failure_and_allows_safe_restage` | Real child exits 42; no native crash |
| Interrupted staging | `test_interrupted_stage_retains_partial_target_and_can_be_repeated` | Injected timeout boundary; no host crash claim |
| Native reload rollback | `test_xray_forced_reload_failure_restores_last_good_native_and_active_config` | Real hosted application/native path; not deployment rollback |
| Native readiness/readback failure | application candidate rejection/readback transport failure cases | Must not publish false success |
| Mandatory VPN / empty pool / protected DIRECT / Emergency Direct | Existing real packet acceptance with added Rescue fixture | Kernel dataplane in owned namespaces |
| Crash during host rollback / system reboot / atomic service activation | NOT RUN | Separate safe host/release qualification required |

The artifact is admitted only after eligible strict aggregate and selected ordinary, Provider/kernel, packet and recovery suites. It is then staged from the actual checked-out source, not only synthetic miniature fixtures. The resulting receipt expressly keeps live activation NOT RUN.

## Fix review and merge handoff

[PR #6](https://github.com/Coxf0rd/fwrouter/pull/6) is the single safety PR. Merge and deployment remain user-owned. Proposed squash title: `fix(network): isolate Rescue LAN and qualify safe backend recovery`. Body: reserve Rescue admission, prove LAN DNS/DHCP/capture boundaries, reject unsafe archived generators, bind content-verified recovery to hosted native/packet evidence, preserve state and host readiness, and cover staged installer failures.

Run38373145947 stopped on four new recovery test defects: three directory link-count false rejections and one old-generator keyword mismatch. Causes were fixed, not allowlisted. First-branch push38372281476 exposed zero-before-SHA selection failure; subsequent push checks pass after immutable merge-base handling. Superseded PR runs cancelled by concurrency are not test PASS. Final required check must be successful on the current PR SHA before user merge.

After user squash: verify actual main event and its admitted artifact, refresh protected backups/current state and host guard proof, finish applicable activation/recovery qualification, then present a separate production GO. No direct-main push, merge, deployment, service restart, DHCP experiment or L6/L7 was performed. Raw evidence and pending post-CI documentation patch remain outside Git.

## Follow-up qualification and infrastructure finding

Run [38376023052](https://github.com/Coxf0rd/fwrouter/actions/runs/38376023052) on59df26c:1393 routine PASS, qualified26+1 PASS, Provider11 PASS and real packet1 PASS. Ordinary25 PASS/1 FAIL: Chromium exited SIGSEGV during first browser launch, before UI/API assertions; other browser cases passed. The warnings about missing D-Bus/cpufreq paths do not establish the root cause. No speculative browser flags, assertion weakening or hidden retries were added. The run is FAIL and no recovery qualification artifact was admitted from it. Reproduction is checked by the final required run, not treated as PASS in this historical receipt.

Final recovery review additionally rejects caller TMPDIR outside canonical `/tmp`, uncompressed-tar violations, overlarge members and total archive bounds before payload reads. Negative regression cases cover the temp-root override and compressed input. The operator-trusted receipt boundary remains explicit; metadata is not independent GitHub authentication.

Future live activation must quiesce not only API jobs but managed services/timers with Wants/Requires on API (subscription refresh, retention, traffic collection and Docker subject events). Otherwise they may reactivate API while installer overlays files. Preserve and restore their exact prior enabled/active state; do not touch unrelated or Rescue services. A bare API stop plus file-copy exit0 is insufficient host rollback proof. This source task performs no such production operation; host activation/quiescence remains a separate qualification and GO gate.

Run38378296221 stopped before native execution:11 recovery cases passed and the compressed-input negative case failed because the BytesIO reader retained transparent decompression. Both reader sites now require `r:` explicitly. The negative assertion is preserved; skipped downstream native steps from this run are NOT RUN.

Final consumer review adds reserved-role checking to `_resolve_lan_ingress_interfaces` as well as fresh discovery: an old cached/supplied preflight projection must not reintroduce Rescue into native LAN ingress rules (including IPv6 LAN blocking). The consumer uses the same central role predicate, preserves other existing names and protected management networks, and has a regression for stale mixed/Rescue-only projections. This does not claim raw old native snapshots are safe restore points.

Run [38379311911](https://github.com/Coxf0rd/fwrouter/actions/runs/38379311911),1686c12:1395 routine +27 qualified native +26 ordinary +11 Provider +1 packet PASS,0 FAIL/0 SKIP. All12 recovery tests and actual full-source artifact staging PASS. Chromium launch failure did not recur; its historical root cause remains unknown, not claimed fixed. The later stale-ingress consumer regression requires the final PR HEAD check; this earlier run does not claim to execute that added case.

## Final source acceptance

[38381629814](https://github.com/Coxf0rd/fwrouter/actions/runs/38381629814), exact source8f9c50c: **1396 routine +26 qualified Mihomo/child +1 qualified Xray +26 ordinary application/browser +11 Provider/kernel +1 packet =1461 PASS /0 FAIL /0 SKIP**. All seven historical failures PASS. Strict source/plan/digest aggregate eligible; no blocked reasons or missing levels. Full-source immutable recovery artifact and real isolated installer stage PASS; stage explicitly reports runtime qualification not established. Final stale/native-ingress regression is executed. The two previous full-source PASS receipts and repeated packet/Provider outcomes are retained; no skipped job or historical browser crash is turned into PASS.

Added15 test functions (12 recovery/admission/staging, two network-role contracts, one DNS reconciliation contract), and strengthened existing discovery and packet acceptance. Physical, custom bridge/VLAN/tunnel names and Rescue-only absence are covered at component depth; native dummy role/discovery/config/DNS/counters/captures/cleanup are verified. DHCP lease exposure, full IPv6 forwarding and Debian host/systemd/reboot remain NOT RUN.

| Area | Status |
|---|---|
| CI milestone | CLOSED |
| External Rescue | PASS — user-confirmed manual external verification |
| Rescue source + stale ingress | PASS |
| Regression/native packet scope | PASS |
| DHCP/DNS safety | PARTIAL: generation/native DNS/capture PASS; DHCP lease exposure NOT RUN |
| Recovery design / admitted artifact / isolated installer | PASS in documented scope |
| Full host rollback activation | BLOCKED / NOT QUALIFIED |
| Production rollback readiness | BLOCKED pending host activation/quiescence qualification and new main target |
| Safety PR | Source ready; final documentation-only HEAD still requires actual required check |
| Production deployment / acceptance | NOT RUN / BLOCKED |
| CD | DEFERRED |

This final documentation/metadata checkpoint changes no application, test or harness code from8f9c50c. The actual required check at [PR6](https://github.com/Coxf0rd/fwrouter/pull/6) remains authoritative for its latest SHA. Its final result is also recorded in the PR description without claiming that a report can pre-certify a future run. GitHub policies are unchanged; no merge or deployment is performed.
