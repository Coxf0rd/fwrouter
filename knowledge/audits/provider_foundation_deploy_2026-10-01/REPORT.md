# Provider-managed foundation deployment and read-only live verification — 2026-10-01

Implementation: `0ba284a64d664af043ca8b7afddcf977c8a23154`. This report distinguishes deployed source, point-in-time observations, isolated diagnostics and pending active-provider acceptance. No production StealthSurf PATCH, provider member switch, protocol change, forced outage, subscription deletion or provider-managed enable was performed.

## Deployment and startup

Authenticated GET `/configs` confirmed current config `309293`, member `1456`, location `26`, protocol `hysteria2`, online. The operator file `/opt/fwrouter-api/.env` was edited atomically to set `FWROUTER_STEALTHSURF_CONFIG_ID=309293`; only that line changed. API key preserved, never printed/logged/committed; file mode 0600. Source and deployed config-loader/import preflight passed before restart; sensitive fields excluded from serialization.

Standard command: `installer/install.sh --deploy --component backend --component ui --component docs`. Only `fwrouter-api.service` explicitly restarted at 16:08:07 Asia/Krasnoyarsk. Existing managed startup subsequently materialized/restarted the Xray container (09:08:39 UTC); Mihomo container was not restarted. No extra runtime unit restart was issued. All API/Mihomo/Xray/subscription-gateway units are active with result success; both containers running. Startup complete, zero traceback/ValidationError/migration exceptions. SQLite schema 22, problem_count 0, no drift. All 47 changed backend/UI files checked against commit blobs match deployed hashes (excluding intentionally preserved `.env.example`).

## Provider state and evidence

Production storage: provider bindings/members/evidence = 0/0/0. Operator credentials are configured, but no subscription has enabled provider management. Subscription and Settings workspace API expose two disabled, nonpersistent source placeholders with Hysteria2 capability, empty members, unconfirmed observed/applied identity and no emergency override. API/UI reads did not establish provider intent or invent fresh evidence.

Explicit authenticated diagnostics used five sequential GET in total: one preflight config read, config/current-location discovery/status reads, and a separate config read for native/client-path validation. No broad inventory crawl or retry loop. The three-request config/discovery/status audit reused discovery once from cache: 5 returned members, 1 discovery network request and 1 cache hit. Current member 1456 was absent from the bounded discovery window. Provider status normalized to up; isolated connectivity succeeded. This directly supports the contract that omission is not DOWN or authoritative deletion. Diagnostic client metrics belong to the separate audit process, not the API server's counters.

No active provider binding, applied provider generation, provider operation job or emergency apply job exists. Active-provider targeted refresh/apply/readback/publication is therefore not live-verified and remains a separate explicit-intent acceptance gate. Shared targeted refresh/generation/selector/recovery contracts are verified by isolated tests.

## Hysteria2 and existing runtimes

Actual authenticated Hysteria2 connection material parsed/normalized through the deployed common subscription/protocol integration. The pinned native Mihomo v1.19.31 binary from image `sha256:ab5d4cf7e192b941f7a238bdb2450d7437d3499b51da1c13669945ba1a138529` accepted an isolated config. A temporary loopback-only native runtime fetched `https://www.gstatic.com/generate_204` through the actual current Hy2 endpoint: HTTP 204, 938 ms. Diagnostic runtime terminated and its 0600 credential config/temporary directory removed; no production runtime configuration or intent changed.

Production Mihomo generated/mounted config hashes match, native validation passes, 77 proxies and zero Hy2 proxies. Current logical selector target matches controller readback; ordinary active VLESS endpoint alive, effective identity available. Zero Hy2 is expected while provider mode is disabled; the diagnostic does not claim an applied production provider path.

Xray generated/mounted config hashes match; native Xray config test returns 0. Generated clients 70, runtime bindings 70, applied bindings 70, failed/pending 0. API Xray and routing/VPN projections are in_sync/healthy. Existing API reports 50 retained stale bindings separately from applied bindings; this count is not treated as a new provider regression. Ordinary subscription state success, last_success_at 08:04:22 UTC; 394 historical memberships, 27 active. Read-only verification did not force a targeted/full refresh or rewrite membership intent.

## Normal API and UI request behavior

Measured API-server provider requests before/after normal selector dry run, active Ping GET, Health, routing/Xray/watchdog state and Settings workspace reads: 0 to 0; all requested endpoints returned HTTP 200 with ok true. Browser Settings/User/Admin render and tab/locale changes at 1440 and 390 px, RU and EN: 39 browser requests, zero mutating requests, zero provider-operation/API requests, unchanged backend provider metrics, no fresh pageerror, no horizontal overflow. Served settings JS/i18n/CSS hashes equal deployed files.

This measured window covers the actual configured-but-disabled live state. Enabled-binding zero-call behavior, candidate eligibility and material handoff are covered by isolated provider tests, not mislabeled as live enabled-provider evidence. Normal API-server provider cache/metrics remain empty; audit-process discovery cache hit is recorded separately. Jobs/events read APIs work and existing background jobs complete successfully. Provider events are registered/localized but no applied-provider success event is expected without an operation.

## Watchdog and Emergency Direct

No live provider recovery or Emergency Direct marker. A transient WATCHDOG_SIGNAL_UNAVAILABLE state was observed after restart; subsequent watchdog state is running/in_sync with no error. At 09:15:44 UTC its newest qualifying dataplane sample was 14 seconds old under a 75-second freshness threshold. Retained snapshot rows do not reconstruct the earlier gap; its transient root cause is unconfirmed. Collector/freshness/gate code is unchanged from 70c524b, with no demonstrated provider regression. Ordinary recovery state was observed without a provider marker.

Three distinct confirmation phases, bounded provider calls, provider unknown status, Direct intent preservation, disabled-client blocking, exact re-entry pre/post probes, failed apply/fallback durable marker and projection/convergence are tested with mocks (27 recovery/projection tests). Production fallback/re-entry was not exercised: no outage, actual switch or provider mutation was induced.

## Post-deploy regressions and limits

Post-deploy source targeted run: 201 PASS across provider/client/storage, targeted refresh, selector, watchdog, recovery/projection. One additional baseline Xray lifecycle test still fails expecting `deleted_count`. UI: 22 Node suites PASS; ux-presentation still fails expecting the superseded Observation section. Both exact failures reproduced on unmodified parent 70c524b; test and relevant implementation files did not change in the provider commit. No corrective implementation commit was warranted.

Full Protocol Adapter stage remains open. Other protocols, full provider/adapter/runtime capability intersection and imported-provider Xray egress/export are the next source block. Active provider enable/apply/publication and production mutation compatibility remain explicit separate acceptance gates. Deployment and the allowed read-only foundation checks are confirmed; active production Provider-managed end-to-end behavior is not claimed.
