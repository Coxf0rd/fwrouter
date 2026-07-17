# `/opt/fwrouter-api/fwrouter_api/routes/subscription.py`

## Назначение

API для subscription URL state, validation, save и refresh pipeline.

## Важные endpoints

- `GET /api/v2/subscription`
- `POST /api/v2/subscription/validate`
- `POST /api/v2/subscription`
- `POST /api/v2/subscription/refresh`

## Внешние зависимости

- subscription service
- subscription pipeline

## Runtime/persistent state

- хранит URL и metadata в `subscription_state`
- refresh может менять server inventory и Mihomo candidate/runtime

## Boot persistence relevance

Средняя/высокая. Provider inventory и generated config paths связаны с post-boot recovery.
