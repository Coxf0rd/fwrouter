# `/opt/fwrouter-api/fwrouter_api/services/watchdog_auto_flow.py`

## Purpose

Automatic scheduler orchestration for the VPN watchdog. The response-traffic
active-quality and stalled-traffic recovery branches live in dedicated handler
modules.

## Important functions

- `run_vpn_watchdog_auto_check(...)`
  Performs module preflight, core-bypass pause, runtime convergence and
  readiness gates, initial automatic selection, traffic-signal analysis, and
  fallback wrapping. It delegates response-traffic quality handling to
  `watchdog_auto_active_quality_flow.py` and stalled-traffic recovery to
  `watchdog_auto_stall_flow.py`.

## Runtime/persistent state

- There are no direct runtime imports.
- Effects go through `WatchdogFlowDeps`: module updates, the runtime controller,
  traffic signals, decision logs, global-mode refresh, and persisted
  cooldown/failure candidates.

## Notes

- Absence of fresh VPN traffic is not an actionable failure.
- Auto/scheduler flow does not pass `log_events` into the legacy manual fallback for idle/healthy outcomes: `FWROUTER_WATCHDOG_SCHEDULER_LOG_EVENTS=true` must not create `vpn_watchdog_no_traffic`/`vpn_watchdog_healthy` heartbeats in the UI/operational journal.
- A full stall is confirmed by the traffic-counter window: outbound-only
  traffic must persist until confirmation.
- Response traffic closes the traffic-failure candidate without failover;
  active-server quality remains diagnostic evidence and is not a trigger.
- Manual selection mode remains monitored but suppresses automatic failover.
- An applied failover starts the persisted cooldown.
