# `/opt/fwrouter-api/fwrouter_api_services_runtime_convergence_scheduler.py`

## Purpose

Generated code-index entry for `/opt/fwrouter-api/fwrouter_api_services_runtime_convergence_scheduler.py`.

## Review Notes

Read the source file directly before changing related behavior. Check adjacent service, route, adapter, script, or systemd documentation as applicable.

## Runtime Impact

This file is part of the FWRouter source/runtime surface. Keep this card synchronized when the file responsibility, runtime side effects, boot relevance, or risk profile changes.

## Guardrails

- Keep FWRouter core as the authority for classification and policy routing.
- Keep Mihomo as a VPN egress adapter, not the network policy engine.
- Preserve direct-safe behavior for host/control-plane traffic unless an explicit scoped contour says otherwise.

## Xray VPN-auto batching

The existing 60-second scheduler also checks the durable `xray.vpn_auto_pending`
marker through `xray_vpn_auto_pending.py` and dispatches the existing JobManager
worker when the trailing quiet deadline or a documented recovery bypass applies.
This does not add a daemon or a second scheduler. Deferred work remains pending
while managed Xray is disabled or authoritative subscription inventory is
unavailable; the next scheduler pass reevaluates it. Failed work obeys its
bounded retry time even when critical drift is present.
