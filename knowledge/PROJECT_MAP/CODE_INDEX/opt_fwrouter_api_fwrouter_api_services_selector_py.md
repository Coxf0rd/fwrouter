# `/opt/fwrouter-api/fwrouter_api/services/selector.py`

## Purpose

Selects and reports the effective `vpn-auto` server from inventory, priority,
cached ping state, optional on-demand checks, and the active `vpn_dataplane`
runtime adapter.

`vpn_auto` membership is broader than automatic selection:

- candidates with `vpn_auto_priority >= 0` participate in auto selection;
- candidates with negative priority remain in inventory/diagnostics and can be
  user-visible without becoming automatic failover targets;
- custom proxy runtime comparison uses its display/server name;
- subscription proxy runtime comparison uses the generated runtime target from
  raw metadata, while persistent state and API responses keep stable
  `server_id`.

Automatic selection ranks only already eligible/healthy candidates with valid
successful runtime ping data. Selector on-demand probes write through the
canonical ping service with semantic source `selector`; they do not overwrite
manual presentation state. It uses weighted latency:
`effective_ping = real_ping / weight`, where `weight` is `1` for priority `0`
or `1`, and then the direct priority value for `2..5`. Priority is therefore a
latency weighting, not a strict rank. `-1` has no effective ping and is
manual-only for automatic selection.

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

When applying a server to Mihomo, selector sends the runtime proxy name. It does
not send subscription `sub:<hash>` IDs as Mihomo proxy names.

## Guardrails

- Keep FWRouter core as the authority for classification and policy routing.
- Keep Mihomo as a VPN egress adapter registration, not a Selector core
  dependency.
- Preserve direct-safe behavior for host/control-plane traffic unless an explicit scoped contour says otherwise.
- On-demand VPN-auto shortlist checks use the generic ordered batch ping path; shortlist construction, ranking, apply, and post-check contracts remain unchanged.
