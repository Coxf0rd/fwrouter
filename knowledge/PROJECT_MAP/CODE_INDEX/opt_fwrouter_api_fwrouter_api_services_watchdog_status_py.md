# `/opt/fwrouter-api/fwrouter_api_services_watchdog_status.py`

## Purpose

Status and read-model helper for watchdog. It owns module row updates, routing-global-state reads, routing mode normalization, and cached scoped-VPN-subject detection.

## Runtime Impact

Reads and updates SQLite `modules`, reads `routing_global_state`, and reads subject effective state through existing subject-policy APIs. It does not run probes, switch servers, write logs, or mutate watchdog failure state.

## Guardrails

- Keep this module limited to status/read-model helpers.
- Preserve desired-mode-first routing semantics for watchdog decisions.
- Keep scoped-subject detection cached through `live_probe_cache`.
