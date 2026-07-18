# Update Rules

Главное правило: после любого изменения кода, конфигов, systemd units, nftables, routing logic, install scripts или API нужно точечно обновлять соответствующие файлы в `knowledge/`.

Пользовательские инструкции лежат в корне `knowledge/`. Техническая карта, архитектура, ADR и `CODE_INDEX` лежат в `PROJECT_MAP/`.

## Если изменен systemd unit

- обновить `PROJECT_MAP/SYSTEMD.md`
- обновить `PROJECT_MAP/BOOT_FLOW.md`
- обновить соответствующий файл в `PROJECT_MAP/CODE_INDEX/`
- проверить `PROJECT_MAP/INVARIANTS.md`

## Если изменена логика boot/startup recovery

- обновить `PROJECT_MAP/ARCHITECTURE.md`
- обновить `PROJECT_MAP/RUNTIME_FLOW.md`
- обновить `PROJECT_MAP/BOOT_FLOW.md`
- обновить `PROJECT_MAP/CONFIGS_AND_STATE.md`, если изменились артефакты или директории
- обновить индекс для `bootstrap.py` или связанного файла

## Если изменена nftables-логика

- обновить `PROJECT_MAP/NFTABLES.md`
- обновить `PROJECT_MAP/NETWORK_MODEL.md`
- обновить `PROJECT_MAP/POLICY_ROUTING.md`, если меняются marks/table/priority
- обновить `PROJECT_MAP/BOOT_FLOW.md`, если меняется recovery
- обновить соответствующий ADR, если изменилась архитектура

## Если изменена policy routing логика

- обновить `PROJECT_MAP/POLICY_ROUTING.md`
- обновить `PROJECT_MAP/NETWORK_MODEL.md`
- обновить `PROJECT_MAP/SYSCTL.md`, если меняются kernel prerequisites
- обновить индекс для `dataplane-apply.sh` и связанных Python services

## Если изменен Mihomo/Xray contract

- обновить `PROJECT_MAP/MIHOMO.md` или `PROJECT_MAP/XRAY.md`
- обновить `PROJECT_MAP/NETWORK_MODEL.md`
- обновить `PROJECT_MAP/SYSTEMD.md`, если меняются units/readiness checks
- обновить `PROJECT_MAP/CODE_INDEX/` для соответствующих compose/service/adapters файлов

## Если изменены install/setup scripts

- обновить `INSTALL_AND_DEPLOY.md`
- обновить `PROJECT_MAP/PROJECT_TREE.md`
- обновить соответствующий `PROJECT_MAP/CODE_INDEX/*.md`
- проверить `PROJECT_MAP/INVARIANTS.md`

## Если изменен API/CLI

- обновить `API_AND_CLI.md`
- обновить `PROJECT_MAP/QUICK_START_FOR_AGENTS.md`, если сменились основные entrypoints
- обновить индекс для route/service entrypoints

## Если изменена схема БД или DB-модель

- обновить `PROJECT_MAP/DATABASE_SCHEMA.md`
- обновить `PROJECT_MAP/PROJECT_TREE.md`, если появились новые schema/runtime files
- обновить `PROJECT_MAP/CODE_INDEX/opt_fwrouter_api_fwrouter_api_db_connection_py.md`
- обновить `PROJECT_MAP/CODE_INDEX/opt_fwrouter_api_fwrouter_api_db_schema_sql_md.md`
- обновить `PROJECT_MAP/CODE_INDEX/opt_fwrouter_api_fwrouter_api_db_schema_state_py.md`
- проверить `PROJECT_MAP/ARCHITECTURE.md` и `PROJECT_MAP/INVARIANTS.md`, если изменился source of truth или lifecycle state

## Если добавлен новый важный файл

- добавить его в `PROJECT_MAP/PROJECT_TREE.md`
- добавить новый файл в `PROJECT_MAP/CODE_INDEX/`, если он влияет на boot, routing, apply, config generation или runtime orchestration
- при необходимости обновить ADR
