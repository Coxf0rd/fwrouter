# `/opt/fwrouter-api/fwrouter_api/db/schema.sql`

## Назначение

Каноническое определение SQLite schema версии `7`.

## Важные сущности

- singleton state: `routing_global_state`, `rules_state`, `subscription_state`
- subject model: `subjects` + detail tables + overrides
- server model: `servers`, `server_preferences`, `server_ping_state`, `server_custom_https_proxy`
- apply/jobs/logging: `apply_versions`, `jobs`, `operational_logs`
- subscription plane: `subscription_accounts`, `subscription_clients`
- traffic accounting: `traffic_counter_snapshots`, `traffic_monthly`

## Внешние зависимости

- `db/connection.py`
- `db/schema_state.py`
- практически все services, работающие с SQLite

## Runtime/persistent state

- определяет структуру persistent state в `/var/lib/fwrouter-v2/fwrouter.db`

## Boot persistence relevance

Критическая.

## Нюансы

- файл не только создает таблицы, но и seed'ит `schema_version` и builtin `modules`
- singleton tables с `id = 1` нельзя размножать
