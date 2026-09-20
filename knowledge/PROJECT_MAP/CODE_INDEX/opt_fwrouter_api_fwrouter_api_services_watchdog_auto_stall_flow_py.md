# `/opt/fwrouter-api/fwrouter_api/services/watchdog_auto_stall_flow.py`

## Назначение

Handler ветки automatic watchdog, когда traffic signal подтверждает outbound-only VPN traffic без response bytes.

## Важные функции

- `handle_stalled_traffic_auto_flow(...)`
  Подтверждает hard traffic stall, suppress-ит failover при pending/manual/cooldown/no adapter и запускает runtime failover при confirmed stall.

## Runtime/persistent state

- прямых runtime imports нет
- эффекты идут через `WatchdogFlowDeps`: hard confirmation state, runtime controller, module state, cooldown, decision logs, global mode refresh

## Нюансы

- Первый outbound-only snapshot только pending; failover возможен после confirmation window.
- After confirmation, the flow requests member reselection through the generic
  runtime adapter and persists a path/target/decision keyed pending phase.
  The next scheduler observation completes recovery only on fresh response
  traffic; a new stalled observation proceeds to full adapter health refresh
  for all vpn-auto logical servers/members, then invokes the existing selector.
  Latency and runtime probe success do not complete recovery.
- После successful applied failover пишет persisted cooldown.
- При отсутствии working candidates возвращает `fail_open_direct_recommended`.
