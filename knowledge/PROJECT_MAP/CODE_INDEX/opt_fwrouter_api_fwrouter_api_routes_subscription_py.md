# `/opt/fwrouter-api/fwrouter_api/routes/subscription.py`

## Назначение

API для subscription URL state, validation, save, batch inventory import и refresh pipeline.

## Важные endpoints

- `GET /api/v2/subscription`
- `POST /api/v2/subscription/validate`
- `POST /api/v2/subscription`
  - legacy payload `{url}` сохраняет один URL как desired state без inventory/runtime refresh
  - UI payload `{urls: [...]}` выполняет batch import нескольких subscription URL: trim/ignore empty/dedupe, download/parse each URL, sync server inventory once by the union of parsed servers, then runs one existing Mihomo candidate/validate/reconcile pipeline so imported servers become pingable/selectable without a separate manual refresh; returns aggregate result and per-URL item status
- `POST /api/v2/subscription/refresh`

## Внешние зависимости

- subscription service
- subscription pipeline

## Runtime/persistent state

- хранит URL и metadata в `subscription_state`
- batch import меняет `subscription_state`, server inventory and then may update Mihomo candidate/active config/runtime through the existing subscription pipeline once for the whole batch
- refresh может менять server inventory и Mihomo candidate/runtime

## Boot persistence relevance

Средняя/высокая. Provider inventory и generated config paths связаны с post-boot recovery.
