# `/opt/fwrouter-api/fwrouter_api/services/watchdog_manual_flow.py`

## Назначение

Manual/runtime-level VPN watchdog check. Используется для ручной проверки и как fallback внутри automatic flow.

## Важные функции

- `run_vpn_watchdog_check(...)`
  Проверяет runtime readiness, idle/no-traffic state, initial auto selection, active target probe и failover candidate/apply path.

## Runtime/persistent state

- прямых runtime imports нет
- эффекты идут только через `WatchdogFlowDeps`: runtime controller, `set_global_mode`, operational events

## Нюансы

- Idle/no traffic не считается ошибкой.
- External runtime без failover/probe не переключается через Mihomo selector APIs.
- При successful failover в `vpn/selective` вызывает global mode refresh через injected `set_global_mode`.
