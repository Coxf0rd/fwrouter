# `/opt/fwrouter-api/fwrouter_api_services_watchdog.py`

## Purpose

Background VPN-auto watchdog service. It updates the `watchdog` module state and may switch the active auto VPN server only after safe traffic-based failure detection.

## Behavior Notes

- Automatic scheduler ticks default to `FWROUTER_WATCHDOG_AUTO_INTERVAL_SECONDS=60`.
- `detect_recent_vpn_traffic_attempts(...)` reads `traffic_counter_snapshots` for `path='vpn'`, but only counts role-based watchdog signals: FWRouter-owned `nftables` VPN dataplane counters for subject types from `subject_taxonomy.TRANSPARENT_INGRESS_CLIENT_SUBJECT_TYPES` + `SYSTEM_SCOPED_SUBJECT_TYPES`, `fwrouter:global:vpn`, or samples with explicit `metadata.watchdog_signal`. Adapter fallback is accepted only for the `external_vpn_module`/`vpn_module` role, not for external management or network-source clients. Runtime/API stats from external modules, for example `xray:subject:*`, remain traffic accounting but are not transparent dataplane health evidence. Legacy `fwrouter:global:vpn` nft mark bytes are interpreted as outbound dataplane traffic for watchdog decisions, not response bytes.
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
