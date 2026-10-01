# Protocol Integration Contract

## Scope

`adapters/protocol_integration.py` is the small endpoint capability and
normalization extension point used by subscription import. It describes
protocol parsing and projection support; it does not register runtime adapters,
perform health probes, or replace the active runtime adapter capability
registry.

An integration definition carries source import formats, Mihomo projection and
native-validation requirements, native Xray egress projection support, public
profile export formats and scope, health/latency eligibility after application,
and a per-entry transport-validation boundary. Optional URI and Xray outbound
parse hooks plus a Mihomo projection hook are dispatched from the registered
definitions. Each definition owns its parse, normalize, validate, and projection
rules; the generic dispatcher does not contain protocol-specific field names.
An unhandled format result falls through to the existing supported parser path.

Integrations use the existing `SubscriptionServer` identity and raw endpoint
model. They do not create a parallel inventory or health model. A validation
failure marks the provider refresh failed so its prior source membership stays
last-good; valid entries from the same failed provider do not authorize removal.

## Current executable integrations

VLESS (plain/TLS/REALITY) is a registered family definition. Its URI and structured
Xray JSON hooks produce the existing canonical Mihomo proxy representation;
Clash/Mihomo YAML entries enter through the existing mapping parser and its
Mihomo projection hook. Base64 URI payloads use the same URI dispatch. All
these paths share REALITY short-ID validation.
Absent and empty short IDs are supported. An explicit YAML/JSON null optional
short ID is normalized to omission; the literal string `"null"` is not
converted to empty. Invalid values produce a safe field/code diagnostic without
logging the value.

The imported endpoint is projected to Mihomo and must pass the pinned native
validator before application. Native Xray egress projection of imported
provider endpoints is not implemented. Existing public Xray subscription
exports describe FWRouter's own inbound client profile and are a separate
contract; they do not promise a lossless export of imported provider REALITY
endpoints. Canonical health and latency become applicable only after the
endpoint is applied through the active VPN runtime adapter.

Hysteria2 is registered for `hysteria2://` and `hy2://` URI imports plus
Mihomo/Clash YAML entries. Its importer maps URI userinfo to Mihomo
`password`, authority host/port to `server`/`port`, `sni` to `sni`, and the
fragment to the display name. It accepts the optional `insecure`,
`obfs`, `obfs-password`, and `pinSHA256` URI parameters and maps them to
Mihomo's `skip-cert-verify`, `obfs`, `obfs-password`, and `fingerprint`
fields. Unknown or duplicate query parameters fail with a sanitized issue.
Endpoint, port, password, optional field types, and the supported
`salamander` obfuscator value are validated before persistence; the generated
proxy still requires validation by the pinned Mihomo binary. Xray egress
projection and public profile export for imported Hysteria2 endpoints are not
implemented. The StealthSurf account evidence observed only `sni`; the other
parameters are supported only as standard URI-to-Mihomo mappings, not claimed
as StealthSurf capabilities.

Mihomo YAML output uses a scoped safe dumper that quotes string scalars whose
plain spelling may resolve as numbers, booleans, or null in another YAML
implementation. It preserves string values and leaves actual integer, boolean,
and null values typed. This addresses resolver differences without rewriting
provider data or changing global YAML parsing behavior.

## Downloaded client-profile selection

Subscription HTTP profiles may return different formats for the same URL. Selection first requires successful parsing, then uses the existing format/count ranking among usable responses. If none parse, the former ranked failure diagnostics are retained. Refresh reuses the selected parsing result. This allows the StealthSurf connect endpoint's supported Hysteria2 Clash YAML to win over recognized but unsupported Xray JSON without adding JSON protocol support or closing the full Protocol Adapter stage.

## Extension boundary

New protocol/security support can add local format hooks and focused
parser/projection tests without adding format-specific branches to the generic
URI and Xray outbound dispatchers. VLESS URI/JSON parsing now lives entirely in its family adapter; Trojan, SS2022 and WireGuard/AWG2 have their own registered hooks. Other protocols remain outside the current provider intersection.
Migration of the remaining protocols, including Hysteria v1 and TUIC, remains
a separate roadmap follow-up. Runtime health operations continue to use the
existing runtime adapter mechanism.

## Approved target architecture / planned migration — 2026-10-01

See [Provider-managed subscriptions and adapters](PROVIDER_MANAGED_SUBSCRIPTIONS_AND_ADAPTERS.md) for the uniform provider/protocol contracts, subscription-level protocol, common persistence owner, available-member selection and bounded watchdog recovery. This supersedes the earlier planned boundary wording retained in roadmap history. Hysteria2 URI/YAML import is implemented in the provider milestone; full protocol migration and provider execution beyond that milestone are not implied here.

Each protocol has its own parser/adapter. The current provider milestone adds Hysteria2 URI/YAML import and pinned Mihomo projection alongside VLESS/REALITY; other exact provider identifiers/formats and runtime intersections remain separate work. Provider-specific protocol names map inside adapters. Only the common pipeline persists canonical domain data; runtime discovery is not intent.

## Authenticated provider/candidate boundary — 2026-10-01

The [canonical provider specification](PROVIDER_MANAGED_SUBSCRIPTIONS_AND_ADAPTERS.md) owns operation budgets, cache classes, rate-limit admission, existing-job single-flight and mutation-material handoff. Its [authenticated audit](/srv/fwrouter/knowledge/audits/stealthsurf_api_2026-10-01/REPORT.md) observed a Hysteria2 URI, no Xray/AWG material, and HTTP/SOCKS5 subconfig capabilities only. Documented primary identifiers remain unobserved, not implicitly supported. Executable integrations above describe the current source contract.

Protocol adapters validate the actual material provided by the targeted fetch stage; sufficient transient provider mutation data does not require another network fetch. Only common persistence publishes verified state. Unapplied provider alternatives have no canonical VPN latency/Health from discovery IPs/slots. The generic candidate execution contract preserves ordinary runtime scoring and requires post-switch validation/readback/connectivity; it is not a second Health model or provider-specific selector. Full remaining protocol migration and pinned-runtime validation remain planned before Performance audit.

## Full supported intersection source checkpoint — 2026-10-01

The [Full Protocol Adapter layer](FULL_PROTOCOL_ADAPTER_LAYER.md) supersedes the earlier planned full-coverage wording for source/tests only. Separate pure family adapters and provider selection profiles cover all eight documented StealthSurf choices with actual generated/native Mihomo v1.19.31 checks, including URI/JSON/YAML/INI where applicable. Subscription protocol execution uses bounded preflight/mutation/confirmation and the existing common targeted pipeline. No deployment or live provider mutation is claimed. Other account variants are official-schema synthetic evidence; only Hysteria2 remains authenticated account material. Imported-provider native Xray egress/export and unsupported protocol/settings combinations remain explicitly open.
