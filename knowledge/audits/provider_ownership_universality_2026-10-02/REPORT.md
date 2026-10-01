# Provider ownership projection and universal protocol dispatch

Date: 2026-10-02 (Asia/Krasnoyarsk). Implementation: `af21855`, deployed over `f15ed43` / `6fcec90`. Protocol implementation remains `dbf2afe`; no protocol adapter architecture or protocol parser source change.

## Findings and correction

The previous ownership predicate followed only direct `subscription_server_memberships`. Materialized server rows representing logical members could therefore lose their source ownership. It now resolves server/member identity to its logical owner and then to the source binding. `source_refs` exposes that provenance in canonical inventory. Current active ordinary/shared ownership remains eligible; provider logical roots stay selectable, internal provider candidates remain internal. Historical memberships remain visible without activating inventory or rewriting preferences.

The reported many-row symptom is not reproduced by current production ownership: the enabled source has Provider vpn plus one historical Norway ordinary entry. The other visible ordinary entries belong to two different source_refs and must remain selectable. They were not guessed into provider ownership from labels or endpoint similarity. Multiple direct legacy entries, inactive historical memberships and inherited member ownership are isolated regression fixtures.

Admin preserves each legacy server's original name, shows the RU/EN managed label, disables Auto/visibility/priority/selection, removes selection/control tooltips and member-expansion actions. Ordinary row affordances remain. User filtering rejects legacy flags even if another inconsistent DTO flag says selectable.

Reverse membership resolution uses two additive indexes on logical_server_members: member_id/logical_server_id and member_runtime_name/logical_server_id. Normal initialize_database creates them; columns/schema version remain 23. A read-only inventory-copy measurement (419 servers/3752 members) reduced the new ownership query from about 2 seconds without indexes to 17 ms with indexes. Deployed read measured 22.7 ms. This is a query measurement, not a whole-API performance claim.

## Protocol universality

Existing dispatch is per entry: URI lines, YAML proxies, and each Xray profile outbound pass through their protocol family. REALITY remains VLESS security; multi-outbound logical-profile grouping is preserved after per-outbound normalization. Mixed fixtures cover VLESS TLS/REALITY, Trojan, Hysteria2, SS2022, WG and AWG2. Equal ordinary/provider material returns equal normalized SubscriptionServer values. Adapter modules do not import provider or subscription ownership implementations; StealthSurf identifiers remain in the provider boundary.

Unsupported schemes/profiles retain supported entries and count unsupported diagnostics. Invalid supported-family options retain existing whole-source FAILED semantics with parsed_count and safe entry diagnostics, protecting last-good; these are distinct canonical outcomes. Ordinary YAML VMess passthrough remains compatible and is not a new family adapter or new verification claim. Full live protocol/handshake verification has not started.

## Tests and review

- Combined backend acceptance: 269 passed, including ownership, selector, custom servers, Admin projection, provider integration/protocol flow, universal dispatch, protocol integration/adapters, native validation and ordinary subscription lifecycle.
- Final added mixed YAML WG/AWG fixture and ownership recheck: 14 passed (overlapping the combined run). There are 270 unique passing backend acceptance cases across these runs.
- Xray regression: 89 passed, same three previously confirmed baseline failures: two full-database snapshot assertions observe watchdog_state initialization; lifecycle expects absent deleted_count. No new failure.
- Seven Node suites passed: Admin/User presentation, UI action integration, common errors, cache/data loading, provider controls and Admin members.
- Final source diff review, whitespace and installer clean-surface checks passed. No credentials or generated artifacts committed.

## Deployment and live evidence

Standard backend/UI/docs installer deployment; preflight passed before restart and deployed loader passed. Only API explicitly restarted at 01:49:49 local; startup completed at 01:50:42. Early probes during startup saw connection refusal and were repeated after readiness; these are not accepted browser evidence. No startup ERROR/Traceback. API, Mihomo, Xray and Xray subscription gateway active. Schema 23 healthy/problem count zero, both indexes installed. Existing .env bytes unchanged. All seven changed deployed backend/UI files match source hashes.

Live Admin/read-model: one actually owned legacy entry, all legacy flags non-selectable/non-Auto, original name and source_refs retained; 27 ordinary selectable rows remain. Provider vpn logical target remains eligible. Selector excludes legacy and retains Provider vpn. Current/applied provider member 1456, binding/applied revision 4 preserved. Canonical topology observes the existing normalized effective member, usable runtime-native health and latency. Routing/VPN/Xray reconciliation in_sync.

Xray canonical generation list: five supported targets, including VPN-auto and three ordinary subscription targets; forbidden legacy/internal intersection zero. Existing custom proxy remains. Global remains subject mode and its UI control is present; no real mode switch exercised. Generated and active container-mounted Mihomo/Xray hashes match; both native validators pass.

Fresh deployed browser checks with GET/HEAD guard: RU/EN at 1440/390, Admin and User, no horizontal overflow, page exceptions, console errors or writes. Legacy selection tooltip absent, member action absent, opacity 0.62, all inputs disabled. Provider group still expands to Norway and five internal members with current/applied/latency/Health presentation. Narrow Settings/locale smoke also passes; Global control retained. Source fixtures cover multiple legacy rows independently, since production has only one.

Local logical ping succeeded. Normal UI/locale/tabs, selector and Health reads left provider metrics requests={} and mutation_outcomes={}; counters unchanged after ping and final UI window. No provider PATCH, protocol change, real server switch, forced outage or destructive subscription operation.

## Remaining boundaries

Three unrelated Xray baseline tests remain. Other-protocol live handshake and provider mutation acceptance gates remain open. Unknown YAML ordinary-runtime passthrough is retained compatibility, not declared new Protocol Adapter support. Production subscriptions/bindings were not fabricated to demonstrate multiple legacy entries.
