# `/opt/fwrouter-api/fwrouter_api/services/watchdog_auto_active_quality_flow.py`

## Purpose

Handles the automatic-watchdog branches for response traffic and bounded idle
active-node observation. Latency and quality are diagnostic evidence here.

## Important functions

- `handle_response_traffic_auto_flow(...)`
  Clears the hard stalled-traffic candidate when response traffic exists,
  performs or reuses the active-target probe, and returns evidence without
  automatic failover. A degraded idle probe also remains observation-only.

## Runtime/persistent state

- There are no direct runtime imports.
- Effects go through `WatchdogFlowDeps`: the probe runtime controller, module
  state, persisted observation state, and decision logs.

## Notes

- The traffic monitor is the only trigger for automatic recovery.
- Response traffic always suppresses automatic recovery regardless of latency.
- Idle latency/probe degradation is recorded as evidence and does not switch
  the server.
