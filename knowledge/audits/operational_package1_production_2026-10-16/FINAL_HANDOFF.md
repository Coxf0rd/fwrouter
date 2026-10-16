# Package 1 production deployment and bounded acceptance — 2026-10-16

Status: **ACCEPTED in the authorized bounded deployment/state scope**. This does not claim full external E2E, Debian host/reboot/rollback or long-soak acceptance.

## Version and deployment

Previous production: main@baafbf209331da763d95bae40c32e4176b158e4d. Deployed: main@3f96e1e73be0e0fc25208a406ecc9ff259f6480f, merged PR13. Full actual main CI [38804147992](https://github.com/Coxf0rd/fwrouter/actions/runs/38804147992) SUCCESS1660/0/0, main smoke and exact-source recovery qualification PASS. PR CI38801349195 also SUCCESS1660/0/0.

Clean Git archive, standard installer `--deploy --component backend --component docs`; all442 backend files verified byte-for-byte after deployment. No local untracked artifacts were deployed. API stopped/started once; Docker subject-event worker and four previously-active API timers temporarily stopped/restored. Gateway was implicitly stopped by its existing Requires=API dependency and explicitly started after healthy API. No Gateway source/units/host installation. All27 protected host files, including Gateway and readiness guard, unchanged. Native Xray/Mihomo container IDs/StartedAt unchanged. No schema migration (25→25), credential change, forced refresh, DNS/firewall/routing setup, reboot or CD.

## Safety and functional evidence

| Gate | Result | Evidence |
|---|---|---|
| Exact installed source | PASS |442 backend files; source export and FINAL_RECEIPT |
| Original identities | PASS |78/78 exact UUID/email pairs in original baseline, native users, generated config and mounted config; durable rows unchanged |
| DB / recovery backup | PASS integrity |schema25, integrity/FK checks; fresh protected SQLite/files/native configs/env archives;9 recovery-object hashes rechecked |
| API / workers / timers | PASS |healthy schema/readiness, stable PID; previously-active dependents restored |
| Gateway / subscriptions | PASS |7/7 API and Gateway HTTP200, identical stored profile payload; no credential mutation or external upstream request |
| Gateway sensitive logs | PASS |canary and real-token/path absence in new logs; expected success/error statuses; explicit loopback proxy bypass |
| Core / native state | PASS observed |routing/VPN/Xray in_sync, healthy projections; controller reads; no DIRECT in mandatory vpn-auto members/selection |
| Routing preservation | PASS |228 normalized owned nft rules and policy rules unchanged; existing VPN connections observed, no active external traffic probe |
| DNS / Rescue isolation | PASS observed |2 configured local answers; Rescue excluded from generated LAN/DHCP/DNS; DNS services and27 protected files preserved |
| Rescue / SSH / Tailscale | PASS preservation |units active, Tailscale Running; prior user-confirmed external Rescue proof retained, no new external handshake claimed |
| Resource / bounded stability | PASS |47/47 healthy, samePID,6/6 bounded parallel reads, no new OOM |
| External Client E2E | NOT RUN |no designated authorized external upstream; no user VPN endpoint used as test |
| Debian host/reboot/rollback | NOT RUN |separate release/staging gate; no destructive test or live rollback |
| Long-soak / production CPU benefit | NOT RUN |7.5-minute observation is not long-soak or controlled causal performance experiment |

## Measurements

Before:13 samples over60.009s; CPU22.696% of one core, RSS sampled max979.02MiB; health median/max4.781/373.106ms.

After:47 samples over460.034s; API CPU user/system119.92/7.0s, 27.589% of one core. RSS initial/final/max673.54/727.16/917.91MiB; lifetimeHWM1045.53MiB; cgroup peak1141858304B; new OOM kills0. Health p50/p95/max5.098/272.4/718.239ms.6 bounded parallel reads,0 errors.

Application startup journal interval26.104s; systemctl start-return to startup complete27.285s. Application cleanup journal interval0.00306s is not total shutdown duration. Normal SIGTERM15, systemd Result=success, application shutdown complete.

Before/after windows have different lengths, warmup and background activity; these numbers do not establish whole-production CPU/RAM savings. Synthetic Package1 equivalence/benchmarks remain the controlled optimization evidence. No timeouts or limits changed.

## Operational procedure findings

Two local coordinator issues were diagnosed without changing FWRouter source or safeguards: an overly narrow exit0 expectation rejected normal successful SIGTERM shutdown before file copying; then a missing HTTP opener in the resumed observer caused polling NameError and a false timeout while API was healthy. Both attempts and actual journal evidence are retained. Completion used independent health/schema checks and native78/durable proof before restoring workers. Gateway's Requires=API stop propagation was explicitly accounted for and its listener/profile delivery verified after restart. An immediate probe before listener readiness was retried after a bounded listener wait. These are procedure observations, not hidden application failures or PASS receipts.

## Recovery and remaining risks

Fresh protected recovery set at /root/fwrouter-recovery/deploy-package1-3f96e1e-20261016; original secrets stay root-only, outside Git/public artifacts. Prior baafbf2 is schema25/security-fix compatible; no pre-fail-closed generator rollback is allowed. Exact-current recovery artifact is source/hosted-isolated-stage qualified; full host activation/rollback remains NOT RUN. No restore/rollback was needed. No backup, historical evidence, credential or legacy Rescue rule was removed.

Existing pending documentation patches remain unchanged. This handoff and a dedicated proposed roadmap sync patch are local/untracked, not published in main. Canonical roadmap receives a dated bounded deployment result; older source/CI checkpoints remain historical. Package2, cache lifecycle, SQLite/I/O optimizations and later stages are NOT STARTED. Observation ended early at user request; the planned full ten minutes and long-soak were NOT RUN. No further implementation/deployment is implied.

Private evidence: FINAL_RECEIPT.json, backup.json/BACKUP_INTEGRITY.json, source-export.json, protected-before.json, DEPLOYMENT_STEPS.json, identities-before.json, final-preservation.json, final-network-summary.json, GATEWAY_ACCEPTANCE.json, STABILITY_SAMPLES.json/STABILITY_SUMMARY.json, BOUNDED_PARALLEL_READS.json, LIFECYCLE_METRICS.json, JOURNAL_SUMMARY.json. Raw DB/configs/logs/profiles remain private under /root/fwrouter-recovery/deploy-package1-3f96e1e-20261016; do not add them to Git.
