# `/opt/fwrouter-api/fwrouter_api_services_runtime_prewarm.py`

## Purpose

Generated code-index entry for `/opt/fwrouter-api/fwrouter_api_services_runtime_prewarm.py`.

## Review Notes

Read the source file directly before changing related behavior. Check adjacent service, route, adapter, script, or systemd documentation as applicable.

## Runtime Impact

This file is part of the FWRouter source/runtime surface. Keep this card synchronized when the file responsibility, runtime side effects, boot relevance, or risk profile changes.

## Guardrails

- Keep FWRouter core as the authority for classification and policy routing.
- Keep Mihomo as a VPN egress adapter, not the network policy engine.
- Preserve direct-safe behavior for host/control-plane traffic unless an explicit scoped contour says otherwise.

## Current source responsibility — Package G qualification follow-up

The source owner is `backend/fwrouter_api/services/runtime_prewarm.py`; the
historical deployment path in this generated card is not current deployment
proof. One process-owned thread opportunistically warms runtime/system/client/
router projections and optionally compiles global profiles, with cooperative
cancellation and live-handle retention. It deliberately excludes the settings
workspace: that projection calls Xray status and may write fingerprinted module
state. Explicit UI/API workspace/status calls retain their existing behavior,
cache TTL and mutation-owned invalidation. Do not weaken Mihomo's generation
fence or remove fingerprint fields to accommodate a background warmer.
