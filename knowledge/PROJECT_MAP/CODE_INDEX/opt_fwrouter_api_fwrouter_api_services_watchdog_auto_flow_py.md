# `/opt/fwrouter-api/fwrouter_api/services/watchdog_auto_flow.py`

## Назначение

Automatic scheduler orchestration flow VPN watchdog. Большие ветки response-traffic active-quality и stalled-traffic failover вынесены в отдельные handler modules.

## Важные функции

- `run_vpn_watchdog_auto_check(...)`
  Выполняет module preflight, core-bypass pause, runtime convergence gate, runtime readiness, initial auto selection, traffic signal analysis и fallback wrapping. Response-traffic quality branch делегирует в `watchdog_auto_active_quality_flow.py`, stalled traffic branch делегирует в `watchdog_auto_stall_flow.py`.

## Runtime/persistent state

- прямых runtime imports нет
- эффекты идут через `WatchdogFlowDeps`: module updates, runtime controller, traffic signal, decision logs, global mode refresh, persistent cooldown/failure candidates

## Нюансы

- Отсутствие свежего VPN-трафика не является actionable failure.
- Auto/scheduler flow does not pass `log_events` into the legacy manual fallback for idle/healthy outcomes: `FWROUTER_WATCHDOG_SCHEDULER_LOG_EVENTS=true` must not create `vpn_watchdog_no_traffic`/`vpn_watchdog_healthy` heartbeats in the UI/operational journal.
- Полный stall подтверждается traffic-counter window: outbound-only traffic должен сохраниться до confirmation.
- Полуживой сервер подтверждается active-server quality window: response traffic есть, но delay-check повторно degraded.
- Manual selection mode продолжает мониторинг, но suppress-ит automatic failover.
- После applied failover включается persisted cooldown.
