# Update Rules

Главное правило: после любого изменения кода, конфигов, systemd units, nftables, routing logic, install scripts или API нужно точечно обновлять соответствующие файлы в `knowledge/`.

## Если изменен systemd unit

- обновить `SYSTEMD.md`
- обновить `BOOT_FLOW.md`
- обновить соответствующий файл в `CODE_INDEX/`
- проверить `INVARIANTS.md`

## Если изменена логика boot/startup recovery

- обновить `ARCHITECTURE.md`
- обновить `RUNTIME_FLOW.md`
- обновить `BOOT_FLOW.md`
- обновить `CONFIGS_AND_STATE.md`, если изменились артефакты или директории
- обновить индекс для `bootstrap.py` или связанного файла

## Если изменена nftables-логика

- обновить `NFTABLES.md`
- обновить `NETWORK_MODEL.md`
- обновить `POLICY_ROUTING.md`, если меняются marks/table/priority
- обновить `BOOT_FLOW.md`, если меняется recovery
- обновить соответствующий ADR, если изменилась архитектура

## Если изменена policy routing логика

- обновить `POLICY_ROUTING.md`
- обновить `NETWORK_MODEL.md`
- обновить `SYSCTL.md`, если меняются kernel prerequisites
- обновить индекс для `dataplane-apply.sh` и связанных Python services

## Если изменен Mihomo/Xray contract

- обновить `MIHOMO.md` или `XRAY.md`
- обновить `NETWORK_MODEL.md`
- обновить `SYSTEMD.md`, если меняются units/readiness checks
- обновить `CODE_INDEX/` для соответствующих compose/service/adapters файлов

## Если изменены install/setup scripts

- обновить `INSTALL_AND_DEPLOY.md`
- обновить `PROJECT_TREE.md`
- обновить соответствующий `CODE_INDEX/*.md`
- проверить `INVARIANTS.md`

## Если изменен API/CLI

- обновить `API_AND_CLI.md`
- обновить `QUICK_START_FOR_AGENTS.md`, если сменились основные entrypoints
- обновить индекс для route/service entrypoints

## Если изменена схема БД или DB-модель

- обновить `DATABASE_SCHEMA.md`
- обновить `PROJECT_TREE.md`, если появились новые schema/runtime files
- обновить `CODE_INDEX/opt_fwrouter_api_fwrouter_api_db_connection_py.md`
- обновить `CODE_INDEX/opt_fwrouter_api_fwrouter_api_db_schema_sql_md.md`
- обновить `CODE_INDEX/opt_fwrouter_api_fwrouter_api_db_schema_state_py.md`
- проверить `ARCHITECTURE.md` и `INVARIANTS.md`, если изменился source of truth или lifecycle state

## Если добавлен новый важный файл

- добавить его в `PROJECT_TREE.md`
- добавить новый файл в `CODE_INDEX/`, если он влияет на boot, routing, apply, config generation или runtime orchestration
- при необходимости обновить ADR
