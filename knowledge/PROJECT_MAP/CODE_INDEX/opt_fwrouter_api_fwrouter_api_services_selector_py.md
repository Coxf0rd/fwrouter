# `/opt/fwrouter-api/fwrouter_api/services/selector.py`

## Purpose

Selects and reports the effective `vpn-auto` server from inventory, priority, cached ping state, optional on-demand checks, and Mihomo runtime state.

`get_vpn_auto_state()` is defensive around Mihomo health: when the controller is unreachable or returns no health object, the API returns a degraded state with `mihomo_runtime_state=failed`, empty selector runtime, `problem_code=mihomo_controller_unreachable`, and `recommended_action=restore_mihomo_runtime` instead of raising a 500.

## Review Notes

Read the source file directly before changing selector behavior. Check adjacent route, Mihomo adapter, server preference, watchdog, and UI log documentation as applicable.

## Runtime Impact

This file can update `routing_global_state.active_auto_server_id`, switch the live Mihomo selector, and write operational logs for successful automatic apply operations. Active auto server state affects effective egress after boot.

## Guardrails

- Keep FWRouter core as the authority for classification and policy routing.
- Keep Mihomo as a VPN egress adapter, not the network policy engine.
- Preserve direct-safe behavior for host/control-plane traffic unless an explicit scoped contour says otherwise.
