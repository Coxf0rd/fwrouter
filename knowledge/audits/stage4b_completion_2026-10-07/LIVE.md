# Stage 4B final deployment and live acceptance

Date: 2026-10-07. Source commit: `7de9f8847e8d29404b97324753df04a1c304d2b5` (includes the reviewed Stage 4B package). This is a point-in-time production receipt; it does not claim a forced refresh, provider mutation, server switch, or outage test.

## Deployment

- Protected pre-deploy backup: `/var/backups/fwrouter/stage4b-final-20261007T090729Z` (created 2026-10-07 09:07 UTC). Directory mode `0700`; config files, SQLite online backup, and manifest mode `0600`. SQLite backup schema 24 and `PRAGMA quick_check=ok`.
- The preflight found no queued, pending, or running jobs. Auto selection revision was 69; active logical target was `Provider vpn`; current/applied provider member was `1456`, protocol `hysteria2`, binding/applied revision 4, last outcome `success`, automatic member switch disabled. Exclusive-source intent was present. Secret values were neither recorded nor emitted.
- Standard deployment command: `cd /srv/fwrouter && ./installer/install.sh --deploy --component backend --component docs`.
- Only `fwrouter-api.service` was explicitly restarted. No package/venv/unit-enable/Docker-network/sysctl step was performed by the installer. Startup took about 44 seconds; service returned active/running and API Health became healthy. No other component was explicitly restarted by this procedure; indirect restarts through existing service dependencies are not asserted here.
- A post-restart failed-units check was reported empty. No unit was reset to achieve that result. The traffic-collector timer/history was not altered.

## Bounded read-only live observation

A passive observer sampled at approximately 0, 63, 124, 186, 246, and 308 seconds after the API became healthy. It made only serial ordinary read requests to Health, VPN-auto selector state, subscription projection, routing state, Xray state, and watchdog state; it also read a bounded SQLite state snapshot. Generated-to-mounted config hashes were compared at the first and final sample. The observer exited successfully after 307.7 seconds.

All six samples had the same relevant state:

- API Health and DB/schema health were healthy.
- `vpn-auto` had exactly one candidate, logical `Provider vpn`; selected target remained valid.
- Active selection hash and provenance-selected hash stayed `cba154af8f72`; reason remained `subscription_refresh_auto_select`.
- Exclusive source hash stayed `2181900775e0`; auto-selection revision stayed 69; no active jobs were present.
- Provider current/applied member remained `1456`, protocol `hysteria2`, binding/applied revision 4, last outcome `success`; automatic member switch remained disabled.
- Routing, watchdog, and Xray projections reported `in_sync`; Xray execution was idle with 80 applied and 80 runtime bindings, zero pending/failed applies. No selection oscillation was observed.
- Provider metrics stayed at zero for requests, discoveries, mutations, cache hits/misses, errors, timeouts, and rate-limit rejections across the observation window.

Final generated/host artifact hashes exactly matched the mounted runtime files:

| Artifact | SHA-256 | Result |
|---|---|---|
| Mihomo | `f4198e36c0673e6be923e31f7fe7921f2f3a6d4a022e11dec3427d99f0ef36b5` | host/runtime parity |
| Xray | `d6435e8585c29c341557d3a7dce129ce83beb456781da7b8a3e34e7ae5fa1ce4` | host/runtime parity |

The separate pinned local Mihomo native validation receipt is [PINNED_MIHOMO_VALIDATION.json](PINNED_MIHOMO_VALIDATION.json): PASS, read-only, no network, no pull. Xray native/readback tests passed in the affected regression gate; the live acceptance here checks deployed host-to-mounted artifact parity and Xray API projection, not a new destructive reload.

The Xray binding counts above describe the post-deploy samples only; this receipt does not claim that the count was unchanged from the pre-deploy baseline.

## Latency scope and limits

The measured `/system/summary` before/after attribution is in [SYSTEM_SUMMARY_BEFORE_AFTER.json](SYSTEM_SUMMARY_BEFORE_AFTER.json): direct-handler observer n=1 per code state, 1,534.5 ms to 1,351.9 ms, same 7,403-byte response; counter reads fell from 285.7 ms to 74.1 ms and native nft invocations from 9 to 5. This is not an HTTP p50/p95 measurement. Latest-deploy end-to-end browser/UI timing and a matched HTTP before/after cohort were not collected; UI code was unchanged. Do not infer frontend improvement from the handler profile.

The revision remained 69 throughout the observation, so there was no startup revision bump to attribute. This does not establish the cause of older unrelated historical revisions or fallback alternations.

## Rollback boundary

Rollback, if required, is code-only to reviewed parent `49591c2` through the standard installer, followed by an API-only restart; do not restore the old SQLite backup over the live DB. Example operator sequence (not executed):

```bash
git -C /srv/fwrouter worktree add --detach /tmp/fwrouter-stage4b-rollback 49591c2
cd /tmp/fwrouter-stage4b-rollback
./installer/install.sh --deploy --component backend --component docs
systemctl restart fwrouter-api.service
```

The protected SQLite/config backup is for recovery investigation and approved restore procedures, not routine code rollback. No rollback was needed.

## Verdict

**Live acceptance PASS for the bounded, non-mutating checks above.** Provider/member, exclusive intent, active/provenance, routing and Xray parity were preserved; provider request counters remained zero; no oscillation was observed. No provider-side mutation or forced outage was performed. Long-window SSD/WAL/storage-growth measurements remain monitoring tails, not blockers established by this acceptance.

## Final backup-to-current artifact comparison

[Receipt](FINAL_ARTIFACT_PARITY.json): pre-deploy and final Xray/Mihomo/nft/applied-manifest bytes match. Bindings raw hash differs only because generated_at and 80 applied_at timestamps were refreshed; all identities/targets/assignments remain identical. This is expected startup receipt metadata, not a fixed-routing change.
