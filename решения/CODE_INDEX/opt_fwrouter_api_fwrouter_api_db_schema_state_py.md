# `/opt/fwrouter-api/fwrouter_api/db/schema_state.py`

## Назначение

Проверяет drift между фактической SQLite schema и ожидаемой contract-схемой.

## Важные функции

- `inspect_database_schema(connection)`
  Сравнивает таблицы, колонки и ключевые SQL snippets с `_TABLE_EXPECTATIONS`.

- `summarize_schema_state(schema_state)`
  Возвращает компактное summary для API.

## Внешние зависимости

- SQLite connection
- `schema_meta`

## Runtime/persistent state

- read-only

## Boot persistence relevance

Высокая. Health/system endpoints используют этот слой, чтобы не работать вслепую при schema drift.

## Нюансы

- coverage expectations здесь не полная для каждого столбца каждой таблицы, а contract-based
- `EXPECTED_SCHEMA_VERSION = 7` должен соответствовать `schema.sql`
