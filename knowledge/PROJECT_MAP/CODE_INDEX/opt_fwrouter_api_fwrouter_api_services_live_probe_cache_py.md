# `/opt/fwrouter-api/fwrouter_api_services_live_probe_cache.py`

## Purpose

Small TTL cache for live probes and derived runtime summaries.

## Review Notes

Read the source file directly before changing related behavior. Check adjacent service, route, adapter, script, or systemd documentation as applicable.

## Runtime Impact

- `get_live_probe_cache(...)` caches a loader result until TTL expires.
- `clear_live_probe_cache(...)` clears every cached runtime summary.
- `clear_live_probe_cache_for_connection(connection_id)` removes only keys scoped to one external connection. External connection probes use keys ending with `.<connection_id>`.

## Guardrails

- Keep FWRouter core as the authority for classification and policy routing.
- Keep Mihomo as a VPN egress adapter, not the network policy engine.
- Preserve direct-safe behavior for host/control-plane traffic unless an explicit scoped contour says otherwise.
