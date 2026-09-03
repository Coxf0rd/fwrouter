# `/opt/fwrouter-api/fwrouter_api/db/migrations.py`

## Назначение

Versioned SQLite migration runner для FWRouter DB. Хранит явную цепочку переходов `N -> N+1` от поддерживаемых legacy schema versions к текущей версии.

## Важные функции

- `run_missing_migrations(connection)`
  Читает `schema_meta.schema_version`, последовательно применяет только недостающие migrations и обновляет version marker после каждого успешного перехода.

- migration functions `7 -> 8`, `8 -> 9`, `9 -> 10`, `10 -> 11`, `11 -> 12`
  Содержат исторические DDL/backfill/rebuild шаги, которые раньше жили inline в `db/connection.py`.

## Runtime/persistent state

- обновляет `/var/lib/fwrouter-v2/fwrouter.db`
- сохраняет existing user intent/data при schema upgrade
- legacy provider detail tables мигрируют в `subjects.metadata_json.detail`

## Нюансы

- Fresh DB не проходит через legacy migrations: `schema.sql` сразу создает актуальную schema/version.
- Runtime/discovered artifacts не превращаются обратно в persistent user intent; migration `10 -> 11` переносит только legacy custom external systems из UI settings.
- Повторный startup после upgrade не выполняет уже примененные migrations, потому что версия уже равна current.
- Regression coverage for schema `10 -> 11 -> 12` checks the official initialization path, `PRAGMA integrity_check`, `PRAGMA foreign_key_check`, preservation of settings/routing/servers/preferences/custom proxy/external connection/subject identity, and idempotent repeated bootstrap.
