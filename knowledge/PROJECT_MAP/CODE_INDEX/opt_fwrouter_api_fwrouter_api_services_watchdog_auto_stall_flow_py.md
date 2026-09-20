# `/opt/fwrouter-api/fwrouter_api/services/watchdog_auto_stall_flow.py`

## Purpose

Handles the automatic-watchdog branch when the traffic signal confirms
outbound-only VPN traffic without response bytes.

## Important functions

- `handle_stalled_traffic_auto_flow(...)`
  Confirms a hard traffic stall, suppresses recovery while pending or in manual
  mode/cooldown/without an adapter, and starts runtime recovery for a confirmed
  stall.

## Runtime/persistent state

- There are no direct runtime imports.
- Effects go through `WatchdogFlowDeps`: hard-confirmation state, the runtime
  controller, module state, cooldown, decision logs, and global-mode refresh.

## Notes

- The first outbound-only snapshot is pending only; recovery requires the
  confirmation window.
- After confirmation, the flow requests member reselection through the generic
  runtime adapter and persists a path/target/decision keyed pending phase.
  The next scheduler observation completes recovery only on fresh response
  traffic; a new stalled observation proceeds to full adapter health refresh
  for all vpn-auto logical servers/members, then invokes the existing selector.
  Latency and runtime probe success do not complete recovery.
- If full refresh does not complete successfully, the persisted phase remains
  `full_refresh_pending` and the selector is not called without fresh health.
- A successful applied failover writes the persisted cooldown.
- If no working candidate exists, the flow returns
  `fail_open_direct_recommended`.
