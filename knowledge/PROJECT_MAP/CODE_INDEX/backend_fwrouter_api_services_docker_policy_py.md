# `backend/fwrouter_api/services/docker_policy.py`

## Purpose

Shared fail-closed evaluator for active Docker Full VPN intents that could
overlap protected infrastructure immunity, plus complete Docker Direct source
coverage when a global VPN/selective policy can capture unmatched packets. It
is called by routing-manifest projection and independently by nft candidate
rendering.

## Guardrails

- Preserve host/FWRouter and exact managed-runtime protection.
- Keep `infrastructure_ipv4` IPv4 addresses and CIDRs in the canonical manifest.
- Reject a conflicting or unready VPN matcher; do not silently convert VPN to
  Direct.
- Direct source inventories must be complete, canonical IPv4 sets (bounded to
  64 addresses), match the primary compatibility address, and carry complete
  IPv6 inventory with no global IPv6 address. Every address must be covered by
  the nft source classifier; conflicts with another active Docker path fail
  closed.
- Keep Full VPN Docker multi-IPv4 unsupported until its own complete routing
  contract is qualified.
- Do not change explicit Core bypass behavior or create another policy owner.
## Docker source-set validation

Complete Docker source coverage is accepted only when the bounded observed-source projection, Direct runtime coverage, and raw matcher array agree. Raw matcher length is bounded before deduplication; canonical string IPv4 values are required. Runtime `bypass_active` is not authority to skip validation; the manifest-level Core bypass owns that behavior.
