# `/opt/fwrouter-api/fwrouter_api_services_watchdog_decision_logs.py`

## Purpose

Decision-log shaping and duplicate-suppression helper for watchdog technical logs.

## Runtime Impact

Writes technical logs only through the provided caller callback. It does not inspect live runtime, mutate watchdog state, or switch servers.

## Guardrails

- Keep log details compact and deterministic.
- Preserve duplicate suppression by fingerprint so recurring watchdog issues do not flood logs.
