# Pass 1 deployment and live acceptance

Application source commit: `f78857f`; baseline `4bb2391`. Date2026-10-08 (+07). Standard backend/docs installer PASS; explicit restart only `fwrouter-api.service`. Existing dependency also restarted the Xray subscription gateway; Mihomo/Xray containers were not restarted/reloaded. Protected SQLite online/config backup: `/var/backups/fwrouter/operational-pass1-20261007T175239Z`,0700directory/0600files,quick_check=ok. No active jobs before delivery.

## State / exact readback

Two samples across 136.8s on the same API process 3820667, plus bounded UI/settings/server/state GETs: API/DB healthy/schema24; routing/selective/auto clean; revision75; exclusive source/provenance/Provider logical target unchanged; current/applied member1456,hysteria2,bindingrevision4, automaticmember-switch disabled. One eligible Provider candidate, same native selected runtime target. No observed oscillation or reconcile state change. This window is bounded, not historical/forced-outage acceptance.

Xray78canonical/applied/verifiedbindings,10handoffs; complete normalized binding digest unchanged. Generated/mounted Mihomo and Xray bytes, applied nft/manifest all exactly match predeploy. Deployed five module files match source. API and gatewayactive/running; runtime oneshotunitsactive/exited; actual containers working. No provider/member mutation/client creation/delete/subscriptionrefresh was triggered for verification.

Native Mihomo validation performed once on a private readonly current-config copy, pinned local image, networknone/CPU1/RAM512MiB/capabilitiesdropped:PASS941.187ms. Current digest differs from the earlier audit receipt; this was the necessary validation, not repeated unchanged validation. Xray config digest and pinned image exactly match the durable nativePASS receipt; reuse it rather than repeat validation. Config validity is not new live protocol-handshake evidence.

## Quantitative provider bracket

Both postrestart samples use the same API process: requests0→0,discoveries0→0,mutations0→0; all cache/error/timeout counters also0. Historical pre-restart requestcount2 is not compared across the reset. Normal Health/router-summary/selector/subscription/Xray/Settings/server/state reads generated no provider polling.

## Latency and resources

Comparable natural serial HTTP `/system/summary` samples: predeploy1288.09/3.81/3.81ms; postrestart1129.57/4.10/3.88ms. Cache age and background conditions were not forced; these are not controlled cold p50/p95 or proof of UI improvement. Post-first response7486B vs7403B reflected the temporary collector failure; final sample shape/size is retained in POSTDEPLOY_2.json. Isolated paired attribution remains the valid call-count/CPU comparison. No operation peakRSS or physicalSSD reduction claim; no new app sleeps/parallelism/cache/indexes.

## Service lifecycle finding / smoke limits

API startup took45s (00:52:50→00:53:35local). The minute collector ran00:53:07 before readiness and curlconnectionfailed. Next regular iteration00:54:08→00:54:09 succeeded automatically; finalfailedunits empty, no reset/restart fix. Existing `After=fwrouter-api` with Type=simple does not wait for HTTP readiness. Record as a Pass2 lifecycle candidate; this package changes no unit/helper/collector. Same readiness boundary explains the temporary additional warning83B.

Isolated L4smokePASS. Formal live-readonly helper cannot fullyaccept: `/state/system` returns HTTP200/ok with a1.65MB state dict, but unchanged smokehelper rejects bodies above32768B. It also requires explicit native/failed-unitinputs not supplied on that invocation. This is a smoke tooling coverage gap, not product path failure or fullsmokePASS. Manual bounded readback, readonlySQL, pinned receipts and finalunits checks above are separate completed L4delivery evidence. No CIlogic altered.

## Rollback

Code-only standardinstaller rollback to detached `4bb2391`, restartAPI; neverrestore oldSQLite over currentDB. Not needed. No persistent intent/schema/runtime-artifact change occurred in this package.

**Verdict: bounded product live acceptancePASS. Formal complete live-smoke tooling gate remains open as described.**
