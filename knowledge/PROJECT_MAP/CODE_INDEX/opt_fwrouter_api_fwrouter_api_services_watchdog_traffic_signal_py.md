# `/opt/fwrouter-api/fwrouter_api_services_watchdog_traffic_signal.py`

## Purpose

VPN watchdog traffic-signal analyzer. It turns recent `traffic_counter_snapshots` rows into the bounded authoritative signal consumed by `services/watchdog.py`.

## Behavior Notes

- Reads only `path='vpn'` snapshots in the requested window and limits the query to recent rows.
- Accepts FWRouter-owned nftables dataplane counters, legacy `fwrouter:global:vpn`, and explicit external VPN-module adapter response signals.
- Ignores generic external management/network-source traffic so UI/runtime accounting cannot accidentally prove transparent dataplane health.
- Builds the current observation around the latest dataplane TX anchor and correlates dataplane RX or explicit adapter fallback RX inside `watchdog_signal_correlation_seconds`.
- Produces stable `decision_id` values from effective TX/RX sample identities for debounce logic.

## Runtime Impact

Read-only SQLite access to `traffic_counter_snapshots`. It does not switch servers, update module state, write logs, or mutate watchdog persistent state.

## Guardrails

- Keep this module traffic-signal-only; failover policy belongs in `services/watchdog.py`.
- Preserve the distinction between raw sample deltas and effective watchdog RX/TX direction for named nft counters.
- Keep external adapter fallback restricted to explicit VPN-module signals.
