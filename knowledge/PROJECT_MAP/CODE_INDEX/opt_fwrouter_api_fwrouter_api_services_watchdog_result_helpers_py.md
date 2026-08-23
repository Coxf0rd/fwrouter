# `/opt/fwrouter-api/fwrouter_api_services_watchdog_result_helpers.py`

## Purpose

Small result-shaping helpers for watchdog paused responses and operational events.

## Runtime Impact

May write operational logs through `write_operational_log`. It does not inspect traffic, update modules, or switch servers.

## Guardrails

- Keep generic result DTO construction here; watchdog policy stays in `services/watchdog.py`.
