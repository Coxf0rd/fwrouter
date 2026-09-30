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

## Current executable integration

VLESS + REALITY is the first registered definition. Its URI and structured
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

Mihomo YAML output uses a scoped safe dumper that quotes string scalars whose
plain spelling may resolve as numbers, booleans, or null in another YAML
implementation. It preserves string values and leaves actual integer, boolean,
and null values typed. This addresses resolver differences without rewriting
provider data or changing global YAML parsing behavior.

## Extension boundary

New protocol/security support can add local format hooks and focused
parser/projection tests without adding format-specific branches to the generic
URI and Xray outbound dispatchers. Existing VLESS non-REALITY, VMess, Trojan,
and other supported inputs remain on their current paths until individually
migrated.
Full parser/projection unification, plus Hysteria/Hysteria2/TUIC support, is a
separate roadmap follow-up. Runtime health operations continue to use the
existing runtime adapter mechanism.

## Approved target architecture / planned migration — 2026-10-01

See [Provider-managed subscriptions and adapters](PROVIDER_MANAGED_SUBSCRIPTIONS_AND_ADAPTERS.md) for the uniform provider/protocol contracts, subscription-level protocol, common persistence owner, available-member selection and bounded watchdog recovery. This supersedes the earlier planned boundary wording retained in roadmap history. The current executable integration described above remains unchanged; full protocol migration and provider execution are not implemented.

Each protocol has its own parser/adapter. The next implementation must verify current StealthSurf API identifiers/formats and pinned runtime capabilities and cover the full supported intersection, including Hysteria2; it must not stop at VLESS/REALITY. Provider-specific protocol names map inside adapters. Only the common pipeline persists canonical domain data; runtime discovery is not intent.
