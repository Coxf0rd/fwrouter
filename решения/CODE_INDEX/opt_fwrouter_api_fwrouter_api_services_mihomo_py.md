# `/opt/fwrouter-api/fwrouter_api/services/mihomo.py`

## Назначение

Service-level facade для Mihomo runtime/config operations.

## Важные функции

Файл служит связующим слоем между routes и специализированными Mihomo modules/adapters. Точные операции зависят от текущих imports и должны сверяться с кодом при изменениях.

## Внешние зависимости

- `adapters/mihomo.py`
- `services/mihomo_config.py`
- `services/mihomo_runtime.py`

## Runtime/persistent state

Может читать live Mihomo controller, generated config и runtime status. Persistent writes обычно проходят через `mihomo_config.py`.

## Boot persistence relevance

Высокая для diagnostics/reconcile, если route/service использует этот facade.

## Нюансы

- Не смешивать live controller state и generated config source of truth.
- Для detailed contract смотреть `MIHOMO.md` и индексы `mihomo_config.py`, `mihomo_runtime.py`, `adapters/mihomo.py`.

