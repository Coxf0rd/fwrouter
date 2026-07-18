# `/opt/fwrouter-api/fwrouter_api/routes/system.py`

## Назначение

Публичные health/readiness endpoints backend.

## Важные endpoints

- `GET /api/v2/health`
  Проверяет DB/schema и выдает service status.

- `GET /api/v2/system/summary`
  Возвращает productized system summary с warnings и readiness.

## Внешние зависимости

- `get_cached_schema_state()`
- `summarize_schema_state()`
- `build_system_summary()`

## Runtime/persistent state

- read-only

## Boot persistence relevance

Высокая. Это базовые post-boot verification endpoints.
