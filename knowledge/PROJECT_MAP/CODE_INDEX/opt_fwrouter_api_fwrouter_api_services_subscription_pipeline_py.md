# `/opt/fwrouter-api/fwrouter_api/services/subscription_pipeline.py`

## Назначение

Многошаговый pipeline для refresh provider inventory и reconcile Mihomo config/runtime.

## Важные функции

- `validate_mihomo_candidate_config()`
- `prepare_subscription_refresh()`
  Internal candidate preparation. It is not a safe public/scheduled mutation
  boundary because inventory persistence precedes runtime promotion.

- `apply_subscription_import_result(refresh_result)`
  Принимает уже синхронизированный subscription inventory result (например batch import), генерирует/валидирует Mihomo candidate config и один раз запускает runtime reconcile без повторного скачивания подписок.

- `apply_prepared_subscription_refresh(prepared)`
  Applies the prepared inventory by promoting/restarting Mihomo only when needed, then running the existing auto-select. After a successful provider inventory result it reconciles managed Xray vpn-auto identities, including for `already_current`. Partial provider failures retain last-good inventory; all-provider failure or synthetic prepared data cannot prune generated identities. The nested profile reconcile is reused, and public snapshots are promoted only after final Mihomo verification.

- `apply_subscription_refresh()`
  Full pipeline with runtime reconcile, Xray profile convergence, and public
  profile promotion only after verification.

## Внешние зависимости

- subscription service
- Mihomo config/runtime services
- Docker image validation
- operational/technical logs

## Runtime/persistent state

- может менять inventory, candidate/active config, Mihomo runtime и managed Xray subscription profile runtime bindings

## Boot persistence relevance

Средняя/высокая. Непрямо влияет на то, какие server inventories и generated configs доступны после boot.

## Runtime reconciliation guard

- Successful refresh requires both prepared and nested provider refresh `ok=true`. A partial batch is eligible because failed providers retain last-good rows; an all-provider failure is not.
- The pipeline reuses the nested profile reconcile returned by the vpn-auto reconciler and preserves final Mihomo verification before public snapshot promotion.
