# `/opt/fwrouter-api/fwrouter_api/services/selector.py`

## Purpose

Selects and reports the effective `vpn-auto` server from inventory, priority,
cached ping state, optional on-demand checks, and the active `vpn_dataplane`
runtime adapter.

`get_vpn_auto_state()` is defensive around runtime health: when the active
adapter controller is unreachable or returns no health object, the API returns a
degraded state instead of raising a 500. Legacy `mihomo_*` response fields and
Mihomo problem codes are preserved for existing clients when the active adapter
is managed Mihomo.

## Review Notes

Read the source file directly before changing selector behavior. Check adjacent
route, runtime adapter registry, server preference, watchdog, and UI log
documentation as applicable.

## Runtime Impact

This file can update `routing_global_state.active_auto_server_id`, switch the
live runtime selector through the active adapter's `apply_server(...)` method,
and write operational logs for successful automatic apply operations. Active
auto server state affects effective egress after boot.

## Guardrails

- Keep FWRouter core as the authority for classification and policy routing.
- Keep Mihomo as a VPN egress adapter registration, not a Selector core
  dependency.
- Preserve direct-safe behavior for host/control-plane traffic unless an explicit scoped contour says otherwise.
