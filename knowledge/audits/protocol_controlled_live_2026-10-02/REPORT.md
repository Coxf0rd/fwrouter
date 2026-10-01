# Full Protocol Adapter controlled live verification

Date: 2026-10-02 (Asia/Krasnoyarsk). Initial deployed baseline: `af21855` / `c8af56c`, including Protocol Adapter implementation `dbf2afe`. A shared watchdog defect found during verification was corrected and deployed as `e9a90d8`.

## Acceptance boundary

All eight profiles passed deployed parser/normalization, capability/provider mapping, common isolated inventory/generated-config/native gates and actual isolated runtime loading. This establishes pinned configuration/runtime construction acceptance, not seven unobserved provider handshakes. Current-account Hysteria2 alone has actual authenticated config and HTTPS endpoint evidence. No provider PATCH, protocol change, manual server switch, forced outage or destructive subscription operation.

| Provider profile | Core family | Native generated validation | Controller runtime type | Provider handshake |
|---|---|---|---|---|
| vless | vless | PASS | Vless | Not verified |
| vless-2410 | vless | PASS | Vless | Not verified |
| trojan | trojan | PASS | Trojan | Not verified |
| trojan-2901 | trojan | PASS | Trojan | Not verified |
| hysteria2 | hysteria2 | PASS | Hysteria2 | HTTPS 204 for existing applied endpoint |
| shadowsocks-2022 | ss | PASS | Shadowsocks | Not verified |
| wg | wireguard | PASS | WireGuard | Not verified |
| amnezia-wg-2 | wireguard, AWG v2 options | PASS | WireGuard | Not verified |

Unobserved account variants use existing pinned official-schema synthetic fixtures, not invented account material. Opaque suffixes imply no guessed security/transport. The seven non-Hy2 profiles remain **runtime validated, live handshake not verified**; current account material cannot be obtained through a protocol mutation without separate permission.

## Parser, capability and runtime evidence

Tests ran from `/opt/fwrouter-api`, explicitly importing modules under that deployed root. `PYTHONPATH=/opt/fwrouter-api`, installed venv, Mihomo v1.19.31. Native fixture tests exercise provider material -> family adapter -> SubscriptionServer -> common isolated SQLite inventory -> production candidate generator -> native `-t`. Applicable URI/base64, JSON, YAML and WG/AWG INI paths pass. Negative security/transport/options/key-length cases reject before mutation/apply, with secret-safe diagnostics. Provider identifiers map at the provider boundary and never become Core protocol types.

Exact eight generated proxies were loaded into a separate Mihomo process with only a temporary loopback controller, no client ingress/TUN/redirect/TProxy listener, no URL-test groups or outbound probes. `/proxies` returned 8/8 expected runtime types. Combined native parse passed before launch. Runtime stopped and temporary directory removed. Repeated after correction deployment with the same result.

Mixed ordinary URI/YAML/JSON sources and VLESS TLS+REALITY are per-entry normalized. Existing multi-outbound logical profile grouping remains after per-outbound normalization. Ordinary/provider material yields identical SubscriptionServer output. Unknown scheme/profile diagnostics retain supported entries; malformed supported-family configuration retains the existing source FAILED/last-good contract. Ordinary YAML VMess passthrough is retained compatibility, not new adapter support.

## Current Hysteria2 handshake and secrets

The concrete proxy selected inside the existing applied Provider vpn group was read from active/generated Mihomo. Its unchanged material was used transiently in a private mode-0700 scratch directory/config mode 0600. An isolated loopback proxy routed only an HTTPS GET through that exact Hy2 endpoint; curl exited 0 with HTTP 204. Repeated after correction deployment, again 204. This proves that endpoint/credential handshake; it is not a claim that all production subjects used Hy2 during the window. Both scratch processes stopped and credential directories were removed. No key/password/config material appears in this report.

One separately budgeted authenticated diagnostic GET `/configs` (budget 1, deadline 25s, fresh read) confirmed config 309293, protocol hysteria2, member 1456 matching binding/applied state; material parsed through the universal layer. This diagnostic ran outside the API process, with per-source credential composition, no persistence/apply. Its one GET is distinct from normal backend/UI request counters. No PATCH and no credential changes.

## Auto transition evidence and correction

Observation before correction: 19 samples, UTC 19:06:17–19:12:26. A real Provider vpn -> Estonia transition occurred at 19:10:43.981784. SQLite provenance decision ID `55258bbb-bca9-44bf-86f3-7dc74ffbf871`, origin watchdog, reason watchdog_initial_select. Operational event `4277998e-53d8-4311-97fb-bc53f8906a66`, code vpn_auto_server_switched, timestamp 19:10:44.373999, reason watchdog_initial_select:scheduler_watchdog_check, source selector. Controller and SQLite both selected Estonia; this was not a UI display issue. No Estonia -> Provider return leg was observed.

Confirmed defect: watchdog used health-qualified active_auto_server_valid as bootstrap target validity. Unknown/manual/failed health could send an existing eligible runtime-mapped target through initial selection rather than the established traffic/failure-confirmation path. The exact health value when this live decision began is not retained; causation specifically by manual ping is not claimed. A normal logical ping occurred in the window and later canonical provider health was healthy/background at 19:10:26.

Correction `e9a90d8` adds active_auto_target_valid for structural eligibility/runtime membership and keeps the existing health-qualified field/ranking semantics. Mihomo watchdog bootstrap uses structural validity. An explicitly present vpn-auto group that omits the active target no longer fabricates membership from a health fallback. Tests prove manual/unknown/failed mapped targets do not initial-select; missing/ineligible/unmapped targets still can. Actual failures retain the existing confirmation/recovery path.

Correction deployed with standard backend/docs installer; only API explicitly restarted at 02:23:01 local, startup complete 02:23:47, no startup ERROR/Traceback. Current Estonia was preserved, not manually restored. Post-correction observation: 13 samples, UTC 19:24:55–19:29:01, including normal ping of current target. SQLite current/provenance and actual runtime vpn-auto selection stable; structural validity true at every sample; watchdog in_sync. Bounded stability evidence does not exclude future or past oscillation.

## Production regression and UI

Provider binding current/applied remain 1456, revision 4; existing provider logical effective member remains usable with runtime-native latency. Global current is Estonia after the observed autonomous transition. Routing/VPN/Xray reconcile in_sync, Health healthy/schema 23, all four FWRouter services active. Generated/active Mihomo and Xray hashes match; both active native validators pass.

Live Settings selected source exposes exactly eight capability-validated profile values with Hysteria2 selected. Account material/handshake availability for the other profiles remains unverified; the selector is the documented provider/adapter/pinned-runtime intersection. RU/EN at 1440/390, Settings/Admin/User: no overflow, page/console errors or writes under a GET/HEAD guard. Settings active-source profile checks repeated after deployment: four language/width combinations, exactly eight options, current Hysteria2, no errors/overflow/writes. Ordinary selectable targets and disabled managed legacy/member exclusions remain intact.

Normal selector, Ping, Health and UI/tab/locale reads leave API-process provider metrics requests={} and mutation_outcomes={}; unchanged throughout both observation windows. The single explicit external diagnostic GET is counted separately. Temporary runtimes do not modify production selectors or routing.

## Tests, commits and remaining gates

- Initial broader regression: 367 passed, same three Xray baseline failures.
- Correction targeted selector/runtime/watchdog: 140 passed.
- Final deployed protocol/parser/native/provider-flow cohort: 71 passed.
- Final deployed ownership/provider/ordinary/selector/watchdog/runtime/Xray cohort: 361 passed, same three Xray baseline failures. Final cohorts total 432 passing backend cases.
- Seven UI suites passed. Extra ux-presentation still fails the previously reproduced obsolete Observation assertion. Three Xray failures remain the two watchdog_state snapshot assertions and absent deleted_count; no new protocol/parser/Xray regression.
- Final correction diff/surface review passed; source clean after commit. Implementation correction `e9a90d8`; this evidence checkpoint is a separate docs commit.

Confirmed gates closed: source, tests, deployment, all-profile native/generated/isolated runtime acceptance, current-Hy2 endpoint handshake and controlled production regression. Seven provider live handshakes, actual protocol mutation/readback and production recovery mutation remain open pending separate authorization. Read-only measured Performance audit can proceed against the stabilized contracts; no full production protocol certification is claimed.
