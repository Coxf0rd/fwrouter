# Provider eligibility correction: deployment and live evidence

Date: 2026-10-01. Implementation: `f15ed43`, including Protocol Adapter source baseline `dbf2afe`. Verification window: API restart at 23:29:36 Asia/Krasnoyarsk through 16:38 UTC and subsequent final UI reads.

## Contract and source review

Enabled provider ownership masks retained ordinary subscription entries at the shared SQL/Python eligibility boundary. Active ordinary/custom shared ownership remains eligible. Internal provider members are matched by canonical member/runtime identity and never become independent top-level targets. Preferences and last-good observed state are preserved. Provider logical roots remain eligible.

Canonical inventory exposes `provider_managed_legacy`, `provider_internal_member`, `auto_eligible`, and `selectable`. Admin explicitly includes missing legacy entries using `include_provider_legacy=true`, with a separate read-cache key. Legacy rows are informational, grey, localized and have disabled selection/Auto/visibility/priority controls. Ordinary rows remain editable even when currently non-selectable. User target filtering, server-override validation, Auto selection, Mihomo group generation and Xray subscription target generation share the boundary. No schema or provider architecture change.

## Tests and review

- 383 targeted backend tests passed: provider eligibility, selector, Admin/configuration/provider-managed and protocol flows, ordinary subscriptions and targeted refresh, Mihomo staged/runtime configuration, watchdog, protocol adapters and pinned native validation.
- Xray regression: 89 passed; three previously reproduced baseline failures remain: two full database snapshot assertions observe a watchdog_state row; lifecycle expects the absent `deleted_count`. These are separate from this correction.
- Seven targeted Node suites passed: Admin/User server presentation, common errors, data-loading/cache behavior, Settings provider controls, Admin provider members, Admin/User UI action integration. Localization and ordinary editable rows covered.
- Final diff review, `git diff --check`, installer clean-surface check passed. No secret/config credential files committed.

## Deployment

Standard `installer/install.sh --deploy --component backend --component ui --component docs`; only `fwrouter-api.service` explicitly restarted. Source config preflight passed; deployed loader confirmed the positive existing config ID. API key/env were unchanged. API startup completed, journal contains no ERROR/Traceback in the deployment window. All API/Mihomo/Xray/Xray subscription gateway units active. Health healthy, schema 23, drift/problem count zero. All 31 backend/UI files changed from deployed `9a151bd` match source hashes.

Both generated files match container-mounted active Mihomo/Xray bytes. Native `/mihomo -t -f /config/config.yaml` and `xray run -test -config /etc/xray/config.json` passed. This also deploys Protocol Adapter source, but establishes live acceptance only for the existing Hysteria2 configuration; other protocols have source/generated/native evidence, not new live handshake evidence.

## Live eligibility, projection and runtime

- `Provider vpn` is the selectable, Auto-eligible logical target of the enabled subscription.
- Its retained Norway ordinary entry remains `missing`, is included in Admin as provider-managed legacy, and has `selectable=false`, `auto_eligible=false`. Existing stored preferences are not rewritten.
- Selector exposes seven eligible logical candidates, including Provider vpn and ordinary subscriptions, excluding the legacy entry/internal members. Active runtime `vpn-auto` still selects `Provider VPN [cba154af8f72]`.
- Provider current/applied member remain 1456, binding/applied revision remain 4. Canonical logical topology has five internal members, one usable current member, fresh runtime-native health/latency. Runtime provider group contains one current transport; it is not an independent global server target.
- Live deployed Xray target-generation read returns five supported targets, including VPN-auto and three ordinary concrete subscription targets; intersection with legacy/internal targets is empty. Existing custom proxy target remains. Unsupported imported provider transports are excluded by the existing Xray capability filter. Global remains a subject mode, not a synthetic concrete server.
- Routing/VPN/Xray canonical reconciliation all report `in_sync`; 70 Xray bindings applied, no missing bindings. Existing stale historical bindings remain separately projected as in the baseline.
- Local logical ping succeeded. Normal selector, Health and UI reads caused zero provider requests: process metrics `requests={}`, `mutation_outcomes={}` before and after the verification window. No provider PATCH, server switch, protocol change or forced outage.

## Browser

Real deployed UI through `127.0.0.1:5500`, existing Chromium/CDP, network guard allowing only GET/HEAD. RU/EN at 1440 and 390 px: legacy row present, localized provider-managed label, grey opacity 0.62, all row inputs disabled; Provider vpn logical row present. User target list retains ordinary candidates and excludes legacy entries. Expanded Provider vpn renders the Norway location with five nested members; current/applied member ID remains secondary detail, no primary VPN 1456 label. Global control retained. Admin/User/Settings render and locale transitions produced zero page exceptions, zero attempted writes and no horizontal overflow.

## Limits

Actual switching/Global mode changes were not exercised because live switching is explicitly prohibited. Existing binding/current/runtime readback and regressions validate the preserved semantics. No destructive production test. Full Protocol Adapter live acceptance for other protocols remains open. Previously recorded unrelated test baselines remain separate follow-ups.
