# `/opt/fwrouter-api/fwrouter_api_services_watchdog.py`

## Purpose

Background VPN-auto watchdog service. It updates the `watchdog` module state and may switch the active auto VPN server only after safe traffic-based failure detection.

## Behavior Notes

- Automatic scheduler ticks default to `FWROUTER_WATCHDOG_AUTO_INTERVAL_SECONDS=60`.
- `detect_recent_vpn_traffic_attempts(...)` reads relevant `traffic_counter_snapshots` for `path='vpn'` and returns aggregate `total_rx_delta`/`total_tx_delta` plus `response_observed`, `outbound_observed`, and `traffic_stalled`. Explicit Xray profile/client counters are excluded so unrelated Xray node traffic cannot mask a Mihomo vpn-auto stall.
- In automatic mode, response bytes (`rx_delta > 0`) mark VPN traffic healthy and do not run an active delay probe.
- Outbound-only traffic (`tx_delta > 0`, `rx_delta == 0`) is treated as a pending stall first. Failover requires a fresh later snapshot and the `FWROUTER_WATCHDOG_TRAFFIC_FAILURE_CONFIRM_SECONDS` confirmation window.
- Re-reading the same stalled snapshot must stay pending; it must not confirm failure or switch servers.

## Review Notes

Read the source file directly before changing related behavior. Check adjacent service, route, adapter, script, or systemd documentation as applicable.

## Runtime Impact

This file is part of the FWRouter source/runtime surface. Keep this card synchronized when the file responsibility, runtime side effects, boot relevance, or risk profile changes.

## Guardrails

- Keep FWRouter core as the authority for classification and policy routing.
- Keep Mihomo as a VPN egress adapter, not the network policy engine.
- Preserve direct-safe behavior for host/control-plane traffic unless an explicit scoped contour says otherwise.
