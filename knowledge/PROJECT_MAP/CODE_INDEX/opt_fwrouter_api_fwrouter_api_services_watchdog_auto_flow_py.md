# `/opt/fwrouter-api/fwrouter_api/services/watchdog_auto_flow.py`

## Назначение

Automatic scheduler decision flow VPN watchdog.

## Важные функции

- `run_vpn_watchdog_auto_check(...)`
  Выполняет module preflight, core-bypass pause, runtime convergence gate, runtime readiness, initial auto selection, traffic signal analysis, confirmed traffic stall, active-server quality confirmation, manual-mode suppression, cooldown и runtime failover.

## Runtime/persistent state

- прямых runtime imports нет
- эффекты идут через `WatchdogFlowDeps`: module updates, runtime controller, traffic signal, decision logs, global mode refresh, persistent cooldown/failure candidates

## Нюансы

- Отсутствие свежего VPN-трафика не является actionable failure.
- Полный stall подтверждается traffic-counter window: outbound-only traffic должен сохраниться до confirmation.
- Полуживой сервер подтверждается active-server quality window: response traffic есть, но delay-check повторно degraded.
- Manual selection mode продолжает мониторинг, но suppress-ит automatic failover.
- После applied failover включается persisted cooldown.
