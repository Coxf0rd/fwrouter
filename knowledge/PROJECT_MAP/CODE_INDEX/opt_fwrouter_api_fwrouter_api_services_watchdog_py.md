# `/opt/fwrouter-api/fwrouter_api_services_watchdog.py`

## Purpose

Background VPN-auto watchdog service. It updates the `watchdog` module state and may switch the active auto VPN server only after safe traffic-based failure detection.

## Behavior Notes

- Automatic scheduler ticks default to `FWROUTER_WATCHDOG_AUTO_INTERVAL_SECONDS=60`.
- `detect_recent_vpn_traffic_attempts(...)` reads `traffic_counter_snapshots` for `path='vpn'`, but only counts role-based watchdog signals: FWRouter-owned `nftables` VPN dataplane counters with prefixes from `subject_taxonomy.watchdog_nft_subject_counter_prefixes()`, `fwrouter:global:vpn`, or samples with explicit `metadata.watchdog_signal`. Adapter fallback is accepted only for the `external_vpn_module`/`vpn_module` role, not for external management or network-source clients. Runtime/API stats from external modules, for example `xray:subject:*`, remain traffic accounting but are not transparent dataplane health evidence. Legacy `fwrouter:global:vpn` nft mark bytes are interpreted as outbound dataplane traffic for watchdog decisions, not response bytes.
- Traffic health/stall decisions use the latest fresh snapshot group. The wider traffic window remains diagnostic context and freshness guard; older response bytes inside the window must not mask a newer outbound-only snapshot.
- Scoped-client detection uses `subject_taxonomy.subject_follows_global_mode(...)`, so watchdog follows the generic transparent-ingress taxonomy instead of a hardcoded implementation list.
- In automatic mode, response bytes (`rx_delta > 0`) prove the VPN path is alive. When fresh VPN activity exists, watchdog may run or reuse a cached current-server delay-check and treats timeout or latency above `FWROUTER_WATCHDOG_ACTIVE_QUALITY_MAX_LATENCY_MS` as degraded active quality. This is not a standalone ping loop: idle/no-traffic ticks do not run it, and fresh successful delay-check results are reused for their TTL. Response traffic suppresses immediate failover, but repeated degraded current-server checks for the same active server/path can confirm `path_state=confirmed_active_quality_degraded` after `FWROUTER_WATCHDOG_ACTIVE_QUALITY_CONFIRM_SECONDS` and `FWROUTER_WATCHDOG_ACTIVE_QUALITY_BAD_CHECKS`.
- Outbound-only traffic (`tx_delta > 0`, `rx_delta == 0`) is treated as a pending stall first. Failover requires a fresh later snapshot and the `FWROUTER_WATCHDOG_TRAFFIC_FAILURE_CONFIRM_SECONDS` confirmation window.
- Re-reading the same stalled snapshot must stay pending; it must not confirm failure or switch servers.
- Watchdog technical logs are decision/error logs, not a heartbeat. The scheduler must not write every 60-second healthy tick, and `paused_signal_unavailable` remains module status only instead of a UI log entry because no fresh VPN traffic can be a normal idle state. UI-visible events are reserved for actionable switch suppression, applied failover, or scheduler errors, with duplicate suppression.
- Persistent `watchdog_state` row access is delegated to `services/watchdog_runtime_state.py`. Keep failover/traffic decisions here, and keep raw row shape, JSON serialization, and empty-state fallback in the state helper.
- VPN traffic signal analysis is delegated to `services/watchdog_traffic_signal.py`. Keep SQL snapshot loading, watchdog-authoritative counter filtering, response correlation, and `decision_id` construction out of the main decision flow.
- Current-server cached delay quality evaluation is delegated to `services/watchdog_active_quality.py`. The main watchdog should consume its active-check DTO and decide whether degraded quality suppresses switching.
- Watchdog module/routing/scoped-subject status helpers live in `services/watchdog_status.py`.
- Traffic failure debounce, soft active-quality debounce/recovery, persistent failover cooldown, and normalized cooldown response fields live in `services/watchdog_failure_state.py`.
- Decision log detail shaping, fingerprinting, and duplicate suppression helpers live in `services/watchdog_decision_logs.py`.
- Generic result helpers and scheduler thread lifecycle live in `services/watchdog_result_helpers.py` and `services/watchdog_scheduler.py`.
- Manual and automatic watchdog decision flows live in `services/watchdog_flows.py`; this module remains a facade that preserves public/private compatibility hooks used by routes, startup, and tests.

## Review Notes

Read the source file directly before changing related behavior. Check adjacent service, route, adapter, script, or systemd documentation as applicable.

## Runtime Impact

This file is part of the FWRouter source/runtime surface. It exposes compatibility wrappers and delegates manual/automatic decision flows, storage, status, traffic-signal analysis, active-server quality, decision logging, result shaping, and scheduler lifecycle to dedicated watchdog helper modules. Keep this card synchronized when the file responsibility, runtime side effects, boot relevance, or risk profile changes.

## Guardrails

- Keep FWRouter core as the authority for classification and policy routing.
- Keep Mihomo as a VPN egress adapter, not the network policy engine.
- Preserve direct-safe behavior for host/control-plane traffic unless an explicit scoped contour says otherwise.
