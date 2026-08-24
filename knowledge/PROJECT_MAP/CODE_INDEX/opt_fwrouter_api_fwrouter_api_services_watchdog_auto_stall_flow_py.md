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
- После successful applied failover пишет persisted cooldown.
- При отсутствии working candidates возвращает `fail_open_direct_recommended`.
