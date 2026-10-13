# Controlled production deployment and live acceptance — 2026-10-13

## Executive decision

**Deployment completed; Production Live Acceptance BLOCKED.** User explicitly authorized deployment without full Debian/systemd rollback qualification and accepted that risk. Deployment target was exactly `main@a4a7a33edeb7b1dab8e081662d01980b2f652925`. The unsafe archived generator was never restored. No reboot, destructiveL7, host installer, CD, direct-main push, Rescue/SSH/Tailscale update or unrelated Docker recreation occurred.

## Provenance, backup and installation

Before activation: remote main SHA verified, actual main CI38387960860 PASS (1461/0/0), protected recovery artifact content verification PASS, fresh private backup `/root/fwrouter-recovery/20261013T114358Z` schema24/integrityok/FK0 and watched config stability PASS. External Rescue remains PASS by user-confirmed manual external verification; no Codex-observed external handshake is invented.

Prior installed Python snapshot:248 paths matchc1e00b5 / identical-code3d285c3. Host is mixed-content, not assigned a fabricated whole-host commit. A clean exact-SHA Git export excludes all local raw evidence/untracked reports. Source surface check PASS. Standard exported `installer/install.sh --deploy --component backend --component docs` exited0 at11:46:13Z. All434 installer-visible backend files matcha4a7a33 at copy and final verification. Current `.env` and existing Xray config were preserved during copy; readiness helper SHA remained unchanged throughout. Host/UI/native images were not installed.

11 managed unit states were saved. Timers stopped first, then dependent workers/gateway/maintenance and API; no active mutation jobs remained. API was started once through its existing preflight. That existing preflight invokes sysctl handling; it was not replaced or bypassed. API completed startup at11:50:27.727846Z (about254s), then active dependent services/timers were restored at11:52:57Z with no enabled/active state mismatches. No Xray restart; one Core-owned Mihomo restart occurred during startup reconciliation. API NRestarts stayed0.

The120s readiness deadline **FAILED**. API later healthy is a separate result, not a rewrite of that failure. Full rollback qualification remains NOT RUN; this was explicitly risk-accepted rather than manufactured PASS.

## Live results

| Check | Status | Actual evidence / boundary |
|---|---|---|
| Installed source / guard / credentials | PASS |434 file matches; protected inputs/guard preserved |
| Core desired/applied | PASS in positive scope |selective/clean, no error, unchanged user routing intent; actual routing projection confirms runtime/rules and VPN readiness |
| Native Xray identities | PASS |Exact configured/native sets78/78; users not listed publicly |
| Native Mihomo transport | PASS |Selected vpn-auto member unchanged, notDIRECT and a member; native process and real traffic work |
| DIRECT HTTPS | PASS |example.com HTTP200,0.365s post-start sample |
| VPN HTTPS | PASS |Forced vpn-global mixed listener5201, HTTP200,0.229s post-start sample |
| VPN UDP | PASS in tested flow |Real SOCKS UDP DNS response from1.1.1.1, rcode0/two answers; matching native UDP connection hasVPN chain/noDIRECT; WAN header capture0 clear UDP packets to test resolver |
| Mandatory VPN fail-closed | PARTIAL |Current guarded rules/native positive proof and tested UDP no-fallback PASS; no production proxy outage/empty-pool injection |
| Rescue LAN/DNS generation | PASS |Reserved role source active; Rescue VIP absent in generated LAN hostnames and forced DHCP DNS; actual fwrouter.lan DNS returns LAN192.168.0.1 |
| Native LAN ingress role convergence | PARTIAL |Two inherited nft Rescue entries remain: IPv6 LAN block and LAN DNS acceptance. No claim of full native Rescue removal |
| DNS | PASS in local query scope |Router answers fwrouter.lan and example.com; dnsmasq/BIND active, no architecture switch |
| DHCP | PARTIAL |Binding enp2s0/range preserved; advertised DNS only192.168.0.1; client lease renew/exposure experiment NOT RUN |
| Persistence | PASS |Schema24 integrityok/FK0; all four non-revision settings preserved; routing intent unchanged. Auto-selection revision changed under startup recovery while target stayed unchanged |
| API content | PASS for captured reads |router-summary/display/LAN-inventory/Xray clients HTTP200; real routing state HTTP200 |
| API readiness/latency | FAIL |Startup254s vs prior31.129s; lightweight captured GETs2.35–14.99s, routing projection38.275s, Mihomo status timeout90s |
| UI | PARTIAL |Actual FWRouter UI HTML HTTP200, correct title/19 scripts; dynamic browser/admin/save acceptance has no valid live receipt and remains NOT RUN |
| Stability/resources | FAIL |15min/31 health samples:15 success,16 timeout at5s; successful median613.34ms/max3083.384ms. Old API RSS812920KiB; later snapshot4977140KiB (~4.75GiB). MemAvailable minimum6821836KiB; no OOM observed |
| nftables/routing continuity | PASS for observation |Owned rules present, five numeric counters increased; existing policy rules/routes/native forwarding retained. Negative routing transitions NOT RUN |
| External services | PASS passive scope |NPM/Vaultwarden/HA/MQTT/Zigbee/Headscale containers retain long uptimes; no unrelated restart/config update. Full application workflows NOT RUN |
| Rescue | PASS preservation / manual preflight |strongSwan/Rescue SSH/frontend/Tailscale active; no configuration changed. Fresh external client path after deploy NOT RUN |
| Full host rollback/reboot/L7 | NOT RUN |Risk explicitly accepted; archived insecure code is inadmissible |

The UDP packet capture proves absence of clearDIRECT fallback only for the correlated test flow; it is not proof that every LAN DNS/client flow has no leak. No actual DHCP exposure or DNS leak incident is claimed. The server resolver/browser hostname path did not yield valid dynamic UI acceptance; instrumentation failures are not attributed as new application defects. No real settings/selectors/clients were changed to obtain test PASS.

## Confirmed regression and source fix

Nonblocking stack snapshots locate expensive Python `yaml.safe_load` in `MihomoHttpAdapter._vpn_udp_policy_readback` during startup/steady runtime health. Current config20,754,596 bytes with199257 top-level rules,99619 paired UDP guards and corresponding transparent rules. Multiple health/projection callers repeatedly parse it. This confirms an expensive parser component; generation/native rule normalization and concurrency may also contribute, and the full254s is not assigned solely to parsing.

Protected offline bounded same-input benchmark: SafeLoader16.9186s/368284KiBRSS; CSafeLoader1.8588s/255132KiBRSS; full parsed objects identical semantic digest. Minimal fix uses C-safe constructor with safe fallback, no stale cache/guard/fencing/readback bypass. Two tests cover512 paired rules/native parity, stale proof/missing guard, malformed YAML and unsafe constructor rejection under both readers.

[PR #7](https://github.com/Coxf0rd/fwrouter/pull/7), commits44f95d5 +26ddad9. Current source remains undeployed. Actual required [run38396231843](https://github.com/Coxf0rd/fwrouter/actions/runs/38396231843) on26ddad9 PASS:217 routine+26 qualified native=243PASS/0FAIL/0SKIP, aggregate eligible, no missing/blocked gates. Earlier source run38393827054 same result; fast38396225357 PASS. This scoped fix check does not claim a new full application/browser/packet run, or a production latency improvement before rollout. Final doc-only PR HEAD still requires its own actual check.

## Containment and operating state

Traffic collection repeatedly exceeded its40s API deadline. After the observation, **only `fwrouter-traffic-collect.timer` is temporarily stopped**, with enabled configuration unchanged, to prevent additional overlapping requests. Its prior active state is retained in the protected control receipt. Traffic accounting freshness is degraded; no fake clean result/reset-failed is used. Other saved active workers/timers, API/native/DNS/Rescue and unrelated services remain operating. Heavy probes and network reapply stopped after the resource/API regression was confirmed.

Kernel Rescue ingress cleanup is deferred to Core-owned reconcile/apply once the control plane is stable; no manual nft edits or global mode/selector switches are attempted under current overload. The two inherited entries are documented drift, not newly proved DHCP exposure or a new Rescue outage.

## Required next actions

1. User Squash and Merge PR7 after final protected HEAD checks; root does not merge.
2. Verify actual new main CI/source and perform an approved focused backend rollout of that new SHA. No live patch or insecure downgrade.
3. Reassess whole-application latency/RAM/collector after activation. Parser benchmark alone does not close the regression; diagnose remaining normalization/concurrency costs if failures persist.
4. Through existing Core ownership/CAS/native validation reapply current intended policy if needed, then verify old Rescue ingress entries absent while protectedDIRECT/Rescue remain intact.
5. Resume previously active traffic timer only after stable API/native/collector readiness and verify accounting recovery. Repeat impacted15min stability, dynamic browser/API/DNS/client coverage safely.

Current targeta4a7a33 is installed; Overall Production Acceptance **BLOCKED**. CI milestone remains CLOSED; CD DEFERRED. No next milestone started. Full rollback/reboot/manual host gates and full client-side negative DNS/DHCP scenarios remain separately unverified.

## Evidence and publication

Raw configs, credentials, identities, logs/captures, backup and benchmark snapshots remain root-only under the protected recovery directory, never Git/GitHub artifacts. Historical reports/pending documentation patch preserved. This redacted engineering report and current roadmap pointer are published through the same fix PR; a PR report is not evidence that the fix was deployed or merged. Local-source branch changes do not alter installeda4a7a33.

## Final safety containment — supersedes operating-state paragraph above

API RSS continued increasing to6,056,568KiB (~5.78GiB); current mutation jobs were0 and Core remainedclean. To protect memory/network/Rescue, all previously managed API-dependent workers/timers and the API were **temporarily stopped** using their existing systemd units. Native Xray/Mihomo, dnsmasq/BIND, strongSwan, Rescue SSH/frontend and Tailscale remainactive. Subsequent actual DIRECT/VPN HTTPS again HTTP200 (0.390/0.258s), Xray78/78, database integrity/FK PASS. The API hit its existing20s stop deadline and remainsfailed/stopped with `Result=timeout`, MainPID0; no reset-failed/fakehealthy result or manual fault injection was used. No unsafe code/config/DB rollback was attempted. Installed backend remainsa4a7a33; the fix is not deployed.

**Current API/control plane unavailable by safety containment**, UI shell only. Background convergence/failover/accounting are paused, not accepted operationally; native routing continues at the preserved observed state. Exact prior active/enabled unit states are in the protected deployment control receipt. Restore them only after user merge, source-verified focused rollout, readiness/native proof and stable resource validation. This containment is not a completed production rollback or Live Acceptance PASS.

The proposed parser acceleration is one measured contributor fix; whole startup/readback/concurrency and graceful shutdown must be remeasured after activation. Do not assume all timeout/RAM symptoms are solved by the parser benchmark. Last source-equivalent hosted receipt38397418169 onb543713 passed; the final containment documentation HEAD requires its actual protected checks before user merge. Overall Production Acceptance remainsBLOCKED.
