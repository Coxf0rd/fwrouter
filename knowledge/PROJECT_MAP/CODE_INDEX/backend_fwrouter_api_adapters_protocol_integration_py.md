# `fwrouter_api/adapters/protocol_integration.py`

## Purpose

Defines the small immutable extension contract for endpoint protocol and security integrations used during subscription import.

## Contract

Each definition provides optional URI and Xray outbound parse hooks, a Mihomo projection hook, matching, normalization, and validation hooks, plus declarative import, projection, export, and health/latency capability metadata. Generic dispatch iterates a static tuple; unhandled entries fall through to existing parser behavior. Validation failures carry safe codes and field names, never rejected endpoint values, and fail that provider refresh so existing source membership remains last-good.

The first definition owns VLESS REALITY URI/Xray JSON parsing and REALITY short-ID rules; YAML mappings use its Mihomo projection/validation path. Existing VLESS non-REALITY parsing remains on its legacy path. It requires native Mihomo candidate validation and does not claim imported-provider Xray egress projection. It does not replace `runtime_adapters` or perform health checks.

See [PROTOCOL_INTEGRATIONS.md](../PROTOCOL_INTEGRATIONS.md) for capability semantics and the bounded follow-up.
