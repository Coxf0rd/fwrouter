# `backend/fwrouter_api/services/docker_policy.py`

## Purpose

Shared fail-closed evaluator for active Docker Full VPN intents that could
overlap protected infrastructure immunity. It is called by routing-manifest
projection and independently by nft candidate rendering.

## Guardrails

- Preserve host/FWRouter and exact managed-runtime protection.
- Keep `infrastructure_ipv4` IPv4 addresses and CIDRs in the canonical manifest.
- Reject a conflicting or unready VPN matcher; do not silently convert VPN to
  Direct.
- Do not change explicit Core bypass behavior or create another policy owner.
