# `/opt/fwrouter-api/fwrouter_api_services_watchdog_scheduler.py`

## Purpose

Watchdog scheduler thread lifecycle and defensive scheduler-tick wrapper.

## Runtime Impact

Owns the background thread/event/lock singleton and calls watchdog callbacks supplied by `services/watchdog.py`. It does not implement traffic or failover policy directly.

## Guardrails

- Keep scheduler lifecycle separate from failover decisions.
- Preserve defensive exception handling for background ticks so one failure does not kill the scheduler silently.
