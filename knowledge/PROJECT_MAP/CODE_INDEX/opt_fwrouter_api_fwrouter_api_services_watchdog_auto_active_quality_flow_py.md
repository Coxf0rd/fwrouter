# `/opt/fwrouter-api/fwrouter_api/services/watchdog_auto_active_quality_flow.py`

## Назначение

Handler ветки automatic watchdog, когда свежий VPN traffic имеет response bytes, но current-server delay/quality check может быть degraded.

## Важные функции

- `handle_response_traffic_auto_flow(...)`
  Сбрасывает hard stalled-traffic candidate, делает или переиспользует active target probe, подтверждает soft active-quality degradation, suppress-ит failover при pending/manual/cooldown/no adapter и запускает runtime failover при confirmed degradation.

## Runtime/persistent state

- прямых runtime imports нет
- эффекты идут через `WatchdogFlowDeps`: probe/failover runtime controller, module state, soft confirmation state, cooldown, decision logs, global mode refresh

## Нюансы

- Response traffic сам по себе не означает идеальный server quality: полуживой сервер может переключиться после confirmation window.
- Healthy active check вызывает recovery confirmation для soft candidate.
- Manual selection mode мониторится, но automatic failover suppress-ится.
