# Emergency Direct and provider recovery re-entry correction — live evidence

Date: 2026-10-04
Deployed source commit: `30a43a0`
Code baseline before deploy: `a139e66`; documentation baseline: `7ad5fb6`

## Deploy

Pre-deploy read-only checks passed: API health/schema 23, desired/applied SELECTIVE, Auto mode, clean apply state, active logical target matching the enabled Provider binding, exclusive source matching that binding, current/applied Provider member and revision matching, and no active jobs or Emergency Direct marker. The Provider binding remained enabled at revision 4. A private mode-0700 backup was created at `/var/lib/fwrouter-v2/backups/emergency-direct-provider-recovery-20261004T143914Z`; it contains an online SQLite backup (`quick_check=ok`), generated Mihomo/Xray configs, Xray bindings, and a private `.env` copy. The `.env` was byte-identical after deployment.

The standard installer completed with `--deploy --component backend --component docs`; only `fwrouter-api.service` was explicitly restarted. No other service was explicitly restarted. API startup took about 70 seconds, with the service process alive but the listener not ready during initialization. It then logged `Application startup complete`, bound `127.0.0.1:5000`, and passed health checks. This matched the previously observed startup duration; systemd `TimeoutStartSec` is two minutes. No startup exception was observed.

## Live checks

- API health remained healthy with schema 23 and zero schema problems. Mihomo, Xray, API, and Xray subscription gateway units were active. Runtime reported selective dataplane enforcement; Xray reported running, forced VPN ready, and traffic available.
- Generated and mounted config hashes matched: Mihomo `170740fbf729f6c014ce9f5de2d6abddf8e1db8479437f456fa069923092d32c`; Xray `ddd71d3e4f54b81a9df171a06bbdce0ff6112aca312c47efd3db898c7ac46c58`. Native Mihomo validation passed using the image entrypoint `/mihomo -t -f /config/config.yaml`; native Xray validation passed. Xray inbound user count was 78; the binding artifact reported 78 bindings and 10 handoff listeners.
- The deployed bounded recovery reader returned a successful selection snapshot in 76 ms: native active target matched the canonical logical runtime name, and effective member matched the active logical member runtime name. `runtime_incarnation(timeout_seconds=2)` returned an identity in 79 ms. These direct, read-only adapter calls did not exercise Emergency Direct or the writer-guard re-entry path.
- One direct HTTPS request to the public 204 endpoint returned HTTP 204 in 294 ms. One request for the same URL through the current Mihomo `vpn-auto` selector returned a 226 ms delay in 329 ms. No retry was made and no health state was persisted by these requests.
- Read-only Xray binding comparison against the private pre-deploy artifact matched the semantic digest: 78 of 78 bindings were applied, with 10 handoff listeners, before and after deployment. Seven verified public profile snapshots contained 70 unique identities; all 70 were present in the mounted native Xray `vless-ws` identity set of 78. Only counts and set digests were emitted.
- One enabled ordinary, non-Provider source was refreshed once through its source-scoped job. The job completed `success` at `verify`. Provider/exclusive intent, binding revision 4, current/applied member, active logical server, selection provenance, and desired/effective mode remained semantically consistent. Provider process counters remained zero after restart and after this ordinary refresh: no Provider requests, discoveries, mutations, timeouts, or errors.
- Selection revision was 25 in the private pre-deploy database copy and 27 after startup/ordinary refresh. Active target, provenance target and decision, mode, Provider binding/member, and exclusive source remained the same. The two revision increments are temporally associated with the Core startup/refresh interval; the database does not retain per-increment ownership receipts, so individual increments cannot be attributed conclusively. Revision remained 27 throughout the following observation window.
- A 90-second observation at t=0/30/60/90 showed stable health, SELECTIVE desired/applied state, Auto mode, active/provenance target, revision 27, Provider binding/member, no active jobs, no Emergency Direct marker, and zero Provider counters. The existing writer lock was available at all four nonblocking samples. These samples establish availability at those instants; they do not measure production re-entry probe duration or exercise re-entry.

## Scheduled refresh evidence and limits

Before deployment, a natural scheduled subscription refresh completed with `PROVIDER_MATERIAL_HANDOFF_STALE` and left `fwrouter-subscription-refresh.service` failed. The failed unit was not reset. Its recent scheduled failures remain preserved as observations with uncertain attribution; their timing does not prove that this milestone caused them. The natural Provider request counter before the API restart showed four `targeted_refresh GET /configs` requests and zero discovery/mutation calls. The API restart reset process-local counters; they remained at zero through post-deploy observation. The scheduled Provider path was not naturally exercised again after deployment during this verification window.

I initiated no Provider API request, Provider PATCH/member switch, forced Emergency Direct activation, induced outage, or destructive production test. Natural scheduled refresh GETs before the restart are reported above. Production Emergency Direct/re-entry success and failed-connectivity acceptance remain unverified; those paths were covered by the isolated tests recorded in [REPORT.md](REPORT.md). Normal operation showed no Provider polling after restart, and source/tests cover probe placement and bounded adapter reads, but these live observations alone do not prove writer-guard duration under an actual recovery re-entry.

One intermediate operator tool output included raw subscription metadata URLs from a runtime summary response. Those values are not reproduced here. This was an output-redaction hygiene error; no credential fields were queried or copied into this report, and token absence is not claimed.

## Test levels

Source and targeted test results are recorded in [REPORT.md](REPORT.md): L0, L1, L2, L3, L4, and L5 passed; L6 full regression was intentionally not run. This operator did not rerun test suites. Live native config, Xray count, adapter-reader, source-scoped refresh, and 90-second checks above are production verification, not substitutes for those test levels.
