# Full Protocol Adapter layer — source checkpoint, 2026-10-01

Source baseline: provider-managed implementation `9a151bd`, deployed evidence `9b30d98`. This checkpoint changes source and isolated tests only. No deploy, restart, live provider GET/PATCH, server switch or protocol change was performed. Schema remains 23. The commit containing this document is the coherent implementation checkpoint; canonical roadmap records its hash separately.

## Ownership and normalized contract

`adapters/protocols/` owns pure format parsing, protocol validation and normalization. `protocol_integration.py` only dispatches registered hooks and carries immutable capability/issue contracts. Family implementations are VLESS (plain/TLS/REALITY), Trojan, Hysteria2, Shadowsocks 2022 and WireGuard/AmneziaWG v2. Opaque provider variants are separate selection profiles at `stealthsurf_protocols.py`, delegating actual material to the family adapter; variants imply no guessed transport or security.

All adapters return `ProtocolHookResult` / `ProtocolNormalizationResult`, consumed as the existing `SubscriptionServer`. Canonical protocol families are `vless`, `trojan`, `hysteria2`, `ss`, `wireguard`; runtime fields use the Mihomo proxy schema. Provider wire IDs never become runtime protocol types. Only the common subscription pipeline owns inventory/topology persistence, native candidate validation, reconcile/checkpoint/rollback and verified publication. Parsing creates no inventory, jobs, network requests, runtime state or Health. Provider-observed identity, persistent protocol intent and verified local-applied identity remain separate.

| Provider wire ID | Selection profile | Canonical family | Imported material |
|---|---|---|---|
| vless | vless | vless | URI/base64, Xray JSON, Mihomo YAML |
| vless-2410 | vless_variant_2410 | vless | Same family; actual supplied security/transport only |
| trojan | trojan | trojan | URI/base64, Xray JSON, Mihomo YAML |
| trojan-2901 | trojan_variant_2901 | trojan | Same family; actual supplied security/transport only |
| hysteria2 | hysteria2 | hysteria2 | URI/base64, Mihomo YAML; existing baseline preserved |
| shadowsocks-2022 | shadowsocks2022 | ss | SIP002 URI/base64, Xray JSON, Mihomo YAML |
| wg | wireguard | wireguard | WireGuard INI, Mihomo YAML |
| amnezia-wg-2 | amneziawg2 | wireguard + AWG version 2 | AWG v2 INI, Mihomo YAML |

Provider selection is the intersection of documented provider profiles, registered protocol families and the pinned Mihomo v1.19.31 runtime matrix in `protocol_runtime.py`. HTTP/SOCKS subconfigs are not subscription protocols. Settings renders these choices through existing RU/EN localization, without new polling or layout.

## Protocol change lifecycle

The existing job/writer/revision guard checks capability before mutation. A bounded preflight config GET confirms resource/location and extended-state safety. Validated protocol intent is persisted without erasing previous provider observation or applied last-good. One PATCH maps the canonical selection to the provider wire identifier. Sufficient authoritative response material is reused transiently; the documented incomplete response needs one bounded post-mutation GET. Missing protocol/location/config identity is never filled from the request.

The targeted material stage validates actual identity, parses the protocol, validates the family/profile intersection and imports one concrete endpoint into the existing `Provider vpn` logical server. Common validation/persistence/reconcile then performs exact member readback and local connectivity verification before publishing applied evidence. A same-protocol no-op also requires fresh local readback; it issues no provider HTTP requests. No automatic mutation retries or reverse mutation are added.

Timeouts, mismatched/stale readback, malformed material and unexpected secret-bearing exceptions produce safe failed/deferred/unconfirmed results. Common partial/failed/unconfirmed apply semantics retain last-good local runtime and applied identity. Desired/provider-observed/local-applied divergence remains visible. The old discovery window is invalidated after confirmed protocol change. Normal selector/Ping/Health/UI render/tab/locale behavior still makes zero provider calls.

## Validation evidence and limits

Required native tests cover all eight selectable profiles through provider normalization, isolated common SQLite inventory/topology persistence, production candidate generation and actual Mihomo `-t`. Additional URI, Xray JSON and WG/AWG INI material paths use the same generated/native gate. Fixtures are synthetic official-schema examples, with provenance and exact pin in [fixture README](/srv/fwrouter/backend/tests/fixtures/protocol_adapters/README.md). The authenticated account fixtures remain Hysteria2-only; other account protocols/variants were not observed. Native config acceptance establishes schema/construction support, not provider handshake, reachability or live credentials.

Hysteria2/REALITY short-ID behavior and ordinary VLESS XHTTP are preserved. Adapter-side validation rejects unsupported Trojan security/transports, VLESS unknown security/QUIC, SS2022 invalid key lengths/plugins/transport security, unknown AWG version/options and malformed INI. Native `-t` alone can silently accept unknown transport strings, so negative adapter gates are required. Errors expose stable field/code information, not key/password/material values.

Unsupported: retained extended/custom provider settings or structured material with unknown extended-state safety during protocol change; multi-endpoint/custom-routing provider current profiles; AWG v3; unsupported family/security/transport combinations; Hysteria v1/TUIC/other protocols outside this intersection. Confirmed standard structured material (`is_extended_settings_enabled: false`) can change after parse validation. Native Xray egress/export of imported provider endpoints remains unimplemented; existing FWRouter inbound/public Xray generation is separate and regression-tested. Deployment/live mutation acceptance remains open.

## Tests and review

Final gates: **290 targeted backend PASS**, including **20 actual pinned native tests**; **268 broader regression PASS**, with three pre-existing Xray failures; **5 focused UI suites PASS**, full UI **22 PASS** with two baseline failures. Counts belong to these distinct checkpoint runs. Coverage includes parser formats, capability/identifier mapping, actual mock HTTP serialization, all selectable generated/native configurations, secrets isolation, protocol success/failure/partial/unconfirmed/no-op readback, ordinary subscriptions and Provider-managed integration. UI tests cover localized choices and existing zero-polling Settings/Admin rendering.

Three Xray regression failures were reproduced on unchanged `9b30d98` as well as this source: `test_vpn_auto_invalid_stage_keeps_database_and_active_xray_unchanged` and `test_legacy_subscription_get_is_read_only` compare full DB snapshots that acquire `watchdog_state`; `test_vpn_auto_reconcile_removes_stale_identity_and_recreates_one_stable_identity` expects absent `deleted_count`. They are pre-existing and not changed in this milestone. UI `settings-events` also expects an obsolete CSS cache version (`20261001c`), while `ux-presentation` expects an obsolete `Observation` label; both failures reproduce on `9b30d98`. No new confirmed regression was found. Final diff review and installer clean-tree surface check passed.
