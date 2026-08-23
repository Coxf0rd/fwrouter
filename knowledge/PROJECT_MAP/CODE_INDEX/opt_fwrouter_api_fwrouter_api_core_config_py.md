# `/opt/fwrouter-api/fwrouter_api_core_config.py`

## Purpose

Defines backend runtime settings through Pydantic settings and `FWROUTER_*` environment variables.

## Behavior Notes

- `runtime_convergence_scheduler_enabled` and `runtime_convergence_interval_seconds` control the fast dnsmasq/dataplane self-heal loop.
- `runtime_convergence_failure_limit` and `runtime_convergence_cooldown_seconds` bound repeated self-heal failures before scheduler calls enter cooldown.
- `watchdog_auto_interval_seconds` defaults to 60 seconds; automatic VPN-auto watchdog checks are traffic-signal based. Idle/no-traffic ticks do not probe the network. When fresh VPN response traffic exists, watchdog may run or reuse a cached current-server delay-check to classify selected-server quality, but degraded delay-check alone does not switch servers while real response traffic is healthy.
- `watchdog_traffic_failure_confirm_seconds` controls how long outbound-only VPN traffic must remain confirmed by fresh counter snapshots before failover is allowed.
- `FWROUTER_STATE_DIR` switches state/log/run paths for isolated tests.

## Review Notes

Read the source file directly before changing related behavior. Check adjacent service, route, adapter, script, or systemd documentation as applicable.

## Runtime Impact

This file is part of the FWRouter source/runtime surface. Keep this card synchronized when the file responsibility, runtime side effects, boot relevance, or risk profile changes.

## Guardrails

- Keep FWRouter core as the authority for classification and policy routing.
- Keep Mihomo as a VPN egress adapter, not the network policy engine.
- Preserve direct-safe behavior for host/control-plane traffic unless an explicit scoped contour says otherwise.
