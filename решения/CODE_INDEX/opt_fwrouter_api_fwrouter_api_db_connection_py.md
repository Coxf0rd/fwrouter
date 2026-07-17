# `/opt/fwrouter-api/fwrouter_api/db/connection.py`

## Назначение

Управляет SQLite connection lifecycle, schema initialization и встроенными миграциями.

## Важные функции

- `get_db_path()`
- `get_schema_path()`
  Возвращают canonical DB/schema paths.

- `connect()`
  Настраивает SQLite:
  - `foreign_keys=ON`
  - `journal_mode=WAL`
  - `synchronous=NORMAL`
  - `busy_timeout=30000`
  - `temp_store=MEMORY`

- `db_session()`
  Контекстный менеджер с `commit/rollback`.

- `initialize_database()`
  Применяет `schema.sql`, делает встроенные schema migrations и возвращает schema inspection summary.

- `get_cached_schema_state()`
  Кэширует schema inspection на короткое/умеренное время для runtime/status endpoints, чтобы не гонять `executescript + inspect` на каждый read-only API request.

## Внешние зависимости

- settings paths
- `schema.sql`
- `inspect_database_schema`

## Runtime/persistent state

- создает и мигрирует `/var/lib/fwrouter-v2/fwrouter.db`

## Boot persistence relevance

Критическая. Без рабочей БД backend не восстановит intended state после reboot.

## Нюансы

- inline migrations здесь часть runtime contract; менять их нужно осторожно
- WAL mode и busy timeout важны для фоновых jobs и concurrent reads
- schema cache безопасен потому, что schema почти статична во время normal runtime; это не source of truth для routing/apply state
