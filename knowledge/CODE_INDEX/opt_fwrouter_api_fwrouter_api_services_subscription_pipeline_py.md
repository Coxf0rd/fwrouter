# `/opt/fwrouter-api/fwrouter_api/services/subscription_pipeline.py`

## Назначение

Многошаговый pipeline для refresh provider inventory и reconcile Mihomo config/runtime.

## Важные функции

- `validate_mihomo_candidate_config()`
- `prepare_subscription_refresh()`
  Refresh без promote/restart.

- `apply_subscription_refresh()`
  Полный pipeline с runtime reconcile, promote и logging.

## Внешние зависимости

- subscription service
- Mihomo config/runtime services
- Docker image validation
- operational/technical logs

## Runtime/persistent state

- может менять inventory, candidate/active config и Mihomo runtime

## Boot persistence relevance

Средняя/высокая. Непрямо влияет на то, какие server inventories и generated configs доступны после boot.
