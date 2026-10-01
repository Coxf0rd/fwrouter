# Provider-managed explicit intent deployment and live acceptance

Date: 2026-10-01. Implementation: `9a151bd`. No corrective implementation change was required.

## Deployment and migration

Standard installer `--deploy --component backend --component ui --component docs` completed. Source tree and installer surface were clean. Config loader and migration preflight on an isolated live DB copy passed before restart. A protected SQLite backup was retained in the existing backups directory. Schema 22 -> 23 applied through the deployed initialization/migration owner; schema 23, drift/problem count zero. Environment file contents were unchanged, API key never printed or logged. Storage parent is 0700; SQLite and sidecars are 0600.

Only API service explicitly restarted at 21:37:48 Asia/Krasnoyarsk; application startup completed at 21:38:33 without config/migration exceptions. All four required FWRouter units are active/result success. Existing gateway dependency followed the API restart. Standard startup/targeted apply materialized managed runtimes; Mihomo/Xray containers are running. All 33 changed backend/UI/docs files match implementation commit hashes.

## Live credentials and configuration

Three saved sources expose explicit toggles, all disabled before the verification action. Settings controls are hidden before enable; UI render/source/locale reads made no writes and provider request counters remained zero. Schema bootstrap copied the environment credential only to the existing matching StealthSurf binding. The dedicated credential row is configured; public projection and UI password field contain no key. Jobs and operational logs contain zero matches of that stored API key. Explicit replacement control is available; no actual credential replacement was needed.

The operator-authorized toggle enabled the existing selected source. Explicit UI read-only discovery returned one config 309293 and selected it automatically. Settings shows provider/key/resource/protocol/status/refresh configuration, no member-management list. Production has only one actual account/config. Multiple-config selection, multiple-account/source isolation and credential replacement are isolated API/UI test evidence, not additional production acceptance; ordinary sources were not repurposed into artificial provider bindings.

## Initialization and canonical runtime state

UI initialization job `986a48a3-c492-4496-985f-9510735ab32a` ran 14:41:32-14:42:13 UTC and completed success, runtime_verified true. Current provider member remains 1456, location 26, Hysteria2. Binding and applied revision both equal 4. Result outcome is noop for provider identity because the current member was retained; successful local initialization/apply/readback is separately verified.

The existing selector active logical ID is the selected source's provider logical ID; canonical effective member is `sub:b9b268d17dbbfa92764d69afadb8c8b1e2f61ed718423601206f1b777de3c1ec`, matching source-scoped identity for member 1456. Provider applied/current and exact effective readback agree. Canonical health is usable, effective member healthy; observed latency 253 ms at one checkpoint (time-varying). The retained selective routing intent was not changed into global VPN. Routing/VPN/Xray/watchdog reconciliation is in_sync; no Emergency Direct override.

Generated/mounted Mihomo and Xray hashes match. Native config validation passes for both. Mihomo generation contains 78 proxies, one Hysteria2 endpoint. Xray projection retains 70 runtime bindings, 70 applied, zero failed/pending. Partial/unconfirmed/last-good failure behavior is covered by isolated tests; no forced failure or outage was induced live.

## Settings, Admin and provider API behavior

Canonical Admin uses `Provider vpn -> Norway location -> provider members`. Five active visible members at the checkpoint: current 1456 and advertised 1918/2310/2387/2400. Historical missing-window members remain stored separately. Primary labels are localized ordinals; IDs are secondary technical details. Current/Applied/Runtime effective badges, Auto/priority controls, canonical latency and Health render through the existing UX. Member switch controls were not executed.

API process metrics total four authenticated GETs: one explicit config discovery, then enable config/current-scope available servers/locations. No PATCH. Normal selector dry reads, local Ping, Health, workspace/member reads and browser render/tab/locale leave request counters unchanged. No provider polling, switches or protocol changes were induced.

Deployed UI RU/EN at 1440/390: ordinary-source toggles and hidden disabled controls, blank saved-key field, resource selection, configuration-only Settings, canonical Admin location/member grouping and localized states confirmed. No horizontal overflow or JavaScript errors. Normal browser checks issued zero writes. Locale-driven local API reload was allowed to complete before checking rendered member language.

## Tests, limitations and regressions

Post-deploy targeted backend regression: 216 passed. Seven Node suites passed. Earlier source checkpoint expanded regression remains 390 passed; final source cache rerun 53 passed. Multi-config/multi-account, invalid credentials, partial/unconfirmed last-good, recovery/Emergency Direct/re-entry are isolated fixtures/mocks. No production PATCH, provider server switch, protocol change, outage or destructive subscription operation.

Existing baseline failures reproduced separately: Xray lifecycle `deleted_count` KeyError at test line 105; UX presentation obsolete Observation assertion. Neither is fixed or counted as provider regression. No new regression from 9a151bd was confirmed. The full Protocol Adapter stage remains open; current-account Hysteria2 initialization acceptance is complete within the authorized no-provider-mutation scope.
