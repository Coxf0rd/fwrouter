# `/opt/fwrouter-api/fwrouter_api_services_watchdog_flows.py`

## Purpose

Manual and automatic VPN watchdog decision flows. The module owns the branch orchestration formerly embedded in `services/watchdog.py`.

## Behavior Notes

- `WatchdogFlowDeps` carries callbacks from `services/watchdog.py` so existing monkeypatch/test compatibility hooks remain centralized in the facade.
- `run_vpn_watchdog_check(...)` handles manual/runtime-level checks without treating idle traffic as a failure.
- `run_vpn_watchdog_auto_check(...)` handles module preflight, runtime convergence, runtime readiness, initial auto selection, traffic signal states, confirmed stalls, soft active-quality degradation, manual-mode suppression, cooldown, and failover.
- With fresh response traffic, degraded current-server quality first creates a soft candidate and suppresses switching. If bad checks and confirmation time are both satisfied for the same active server/path, the flow may use the normal runtime failover path with `path_state=confirmed_active_quality_degraded`. Good active checks clear the soft candidate through the recovery window.

## Runtime Impact

No direct imports of runtime services. Runtime effects happen through injected dependencies: module updates, runtime controller calls, global mode refresh, operational/technical logs, traffic-signal reads, and watchdog persistent state.

## Guardrails

- Keep low-level storage, signal analysis, active quality, scheduler lifecycle, and log shaping in the dedicated watchdog helper modules.
- Preserve `WatchdogFlowDeps` indirection so tests that patch the facade in `watchdog.py` keep affecting flow behavior.
