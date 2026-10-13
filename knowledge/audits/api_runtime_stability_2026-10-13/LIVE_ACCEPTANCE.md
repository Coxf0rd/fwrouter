# API runtime stabilization — controlled feature-source live acceptance

## Decision — 2026-10-14

**BLOCKED; production control plane remains stopped.** User authorized a backend/docs deployment from qualified feature source before final PR readiness. Exact deployed source is `4ffd6865341a49c3d4693ecfdd86fcb664b6d9b8`, branch `fix/api-runtime-health-scale-stability`; main remains `1b302e9`. This is not a merge or a main deployment. No host/UI/native image installation, reboot, CD, Rescue/SSH/Tailscale modification or unrelated service restart.

## Hosted evidence

[PR run38472054455](https://github.com/Coxf0rd/fwrouter/actions/runs/38472054455) SUCCESS:1439 routine PASS, qualified native26+1 PASS, ordinary26 PASS, Provider11 PASS, packet/scale2 PASS, no failure/skip in executed suites. Aggregate eligible and owned cleanup confirmed. L6/L7 and Debian host qualification are not included.

Scale generated23515102 bytes /199273 top-level rules /99627 VPN guards,78 synthetic native identities retained. Full bootstrap/recovery/Xray/DNS receipt successful. Startup30.0161s; four six-client waves all HTTP200, maximum2.3812s; API process VmHWM892128KiB (not aggregate native memory); completed shutdown0.2137s. This fixture retains78 identities but empty subscription-profile reconciliation does **not** rebuild78 real managed profiles. The live failure exposes that important qualification boundary.

## Backup and rollout

Protected recovery set `/root/fwrouter-recovery/api-stability-20261013T202715Z`: online DB integrityok/FK0; archives fully readable;434 pre-deployment installed files matcha4a7a33; private runtime/Xray/.env snapshots and source exports retained. Fresh pre-activation DB backup and watched native config/guard hash recheck passed. Native78 users and server-side Rescue preservation confirmed. External Rescue remains prior user-confirmed manual PASS, not a newly observed external session.

Standard exact-commit Git export installer `--deploy --component backend --component docs` exited0.436 installed backend files match4ffd686, `.env`/native config/readiness guard unchanged at copy verification. No untracked/raw evidence entered the export. Host scope excluded.

Only API started through its existing systemd preflight. Dependent workers/timers stayed held. Existing preflight includes sysctl handling; it was not bypassed/replaced. API did not reach readiness within120s; sampler stopped it through systemd. Observed maximum RSS751428KiB (~734MiB), versus prior6056568KiB (~5.78GiB). Activation+stop took140.38s: readiness120s FAIL plus existing20s stop timeout. API MainPID0/failed afterwards. This is neither startup success nor graceful shutdown acceptance. No unsafe old generator restored.

## Partial Xray generation — critical unresolved gate

After failed activation native count remains78, but UUID set changes:8 removed/8 added,70 unchanged; all70 common email/account projections unchanged. Seven changed entries in each direction are managed subscription email entries; one is another generated entry. Do not publish identities. Native UUID set equals the78 expected candidate identities in the actual checkpoint, whose phase is `xray_applied`. Final publication did not complete. Desired/applied Core row still selective/clean and DB integrity/FK pass; that alone cannot prove Xray generation consistency.

This establishes a partially applied generation and a preservation failure against the original native identity baseline. It does **not** prove DB corruption or actual user disconnect, which were not measured, and does not yet establish why candidate derivation changed those UUIDs. The live startup bottleneck remains unattributed: no production phase instrumentation; attempted read-only py-spy dump failed because the archived diagnostic executable lacks execute permission, and no live stack was obtained. Do not assign the120s solely to YAML, hashing, Docker or networking.

No further activation, native reload or guessed restore was attempted after this new critical uncertainty. A checkpoint-owned recovery may be appropriate, but must verify artifact integrity, current incarnation/config hashes, source/DB compatibility and identity intent before changing native state. Old pre-a4 generator with implicit DIRECT remains inadmissible. The backed-up a4 code is a fail-closed containment baseline, not a proven stable control-plane rollback.

## Safe preservation observations after stop

| Check | Result |
|---|---|
| Backend provenance / copy / guard | PASS |
| API120s readiness | FAIL |
| API systemd20s stop | FAIL / timed out |
| Original native UUID preservation | FAIL:8 replacements |
| Native candidate/checkpoint parity | PASS for observed candidate; final publication NOT COMPLETE |
| DB integrity/FK | PASS |
| DIRECT HTTPS | HTTP200,0.349567s |
| Native vpn-global HTTPS delay | HTTP200,235ms; not all client-flow acceptance |
| Current vpn-auto | Native member present, not DIRECT/REJECT; target not changed by test |
| Router local DNS | fwrouter.lan→192.168.0.1 |
| DNS/BIND/Rescue/SSH/Tailscale services | Active, unchanged configuration |
| Xray native count |78; count alone does not establish preservation |
| Full UI/admin/save, steady15min, DHCP client renew, DNS leaks | NOT RUN |
| Restore dependent timers/workers | NOT RUN; stay held |
| Full live rollback/host/reboot/L7 | NOT RUN |

No negative outage/fail-closed experiments or manual nft edits were executed. Successful native network probes are positive limited-scope observations, not Production Acceptance.

## Required next work

1. Read-only reconcile the8 changed candidate identities against pre-start DB/native projections and generation-derived UUID contracts; establish whether they are intended migration or a defect and which bindings must be retained.
2. Admit a checkpoint-owned safe recovery to the preserved native baseline if required, keeping Core ownership/CAS/incarnation/native proof; no raw config overwrite or insecure code rollback.
3. Extend isolated hosted scale fixture with actual nonempty subscription accounts/profile nodes matching production cardinality. Current empty-profile case cannot qualify this live path.
4. Add bounded real generation timing/stack evidence and diagnose live-equivalent startup plus cancellation/recovery at partial apply. Improve proven source cause through PR8/required checks; do not raise limits to hide failure.
5. Once qualified, repeat controlled backend activation and full live acceptance, then restore exactly the previously active dependent units only after readiness/native stability. User merge remains separate.

Overall Production Acceptance BLOCKED. CI milestone CLOSED; CD DEFERRED; Operational Performance Fixes not started. Raw evidence protected locally; no secrets or client IDs in this report.
