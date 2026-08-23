# `/opt/fwrouter-api/fwrouter_api_services_watchdog_failure_state.py`

## Purpose

Traffic-failure debounce and failover cooldown helper for watchdog.

## Runtime Impact

Reads and writes persistent `watchdog_state` through `watchdog_runtime_state.py` and keeps an in-process failure candidate cache. It does not read traffic snapshots, choose VPN targets, update modules, or write logs.

## Guardrails

- Keep this module state-only; failover policy belongs in `services/watchdog.py`.
- Preserve restart-tolerant behavior by preferring persistent `watchdog_state.failure_candidate_json` over the in-memory candidate.
- Do not confirm failure from the same stalled snapshot.
