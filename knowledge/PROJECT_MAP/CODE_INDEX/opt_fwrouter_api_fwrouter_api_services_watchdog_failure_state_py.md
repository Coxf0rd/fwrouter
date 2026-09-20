# `/opt/fwrouter-api/fwrouter_api_services_watchdog_failure_state.py`

## Purpose

Traffic-failure debounce, soft active-quality debounce, and failover cooldown helper for watchdog.

## Runtime Impact

Reads and writes persistent `watchdog_state` through `watchdog_runtime_state.py` and keeps an in-process failure candidate cache. It stores both hard stalled-traffic candidates and `kind=active_quality_degraded` soft candidates in the same persisted slot. It does not read traffic snapshots, choose VPN targets, update modules, or write logs.

## Guardrails

- Keep this module state-only; failover policy belongs in `services/watchdog.py`.
- Preserve restart-tolerant behavior by preferring persistent `watchdog_state.failure_candidate_json` over the in-memory candidate.
- Do not confirm failure from the same stalled snapshot.
- Response traffic clears only the traffic confirmation candidate. Runtime recovery phases remain persisted in the watchdog state and survive new observations and restart.
