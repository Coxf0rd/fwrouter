# `/opt/fwrouter-api/fwrouter_api/services/subscription_pipeline.py`

## Назначение

Многошаговый pipeline для refresh provider inventory и reconcile Mihomo config/runtime.

## Важные функции

- `validate_mihomo_candidate_config()`
- `prepare_subscription_refresh()`
  Refresh без promote/restart.

- `apply_subscription_import_result(refresh_result)`
  Принимает уже синхронизированный subscription inventory result (например batch import), генерирует/валидирует Mihomo candidate config и один раз запускает runtime reconcile без повторного скачивания подписок.

- `apply_prepared_subscription_refresh(prepared)`
  Общая часть apply: сравнение candidate/active config, promote/restart только при отличии, auto-select после успешного reconcile.

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
