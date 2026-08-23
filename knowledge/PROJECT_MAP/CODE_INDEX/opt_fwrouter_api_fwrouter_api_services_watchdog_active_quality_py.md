# `/opt/fwrouter-api/fwrouter_api_services_watchdog_active_quality.py`

## Purpose

Current VPN-auto server quality helper for watchdog. It reuses recent successful `server_ping_state` rows and normalizes latency-threshold degradation.

## Behavior Notes

- `recent_successful_active_check(...)` reads cached successful ping state for the active server inside a bounded TTL.
- `active_quality_degraded(...)` treats failed checks as degraded and compares successful latency to `watchdog_active_quality_max_latency_ms`.
- `degraded_active_check(...)` converts a successful but too-slow check into the normalized watchdog degraded DTO.

## Runtime Impact

Read-only SQLite access to `server_ping_state`. It does not run probes, switch servers, update module state, or write logs.

## Guardrails

- Keep this module cached-quality-only; active probe execution and failover policy stay outside it.
- Do not make idle watchdog ticks start active checks from here.
