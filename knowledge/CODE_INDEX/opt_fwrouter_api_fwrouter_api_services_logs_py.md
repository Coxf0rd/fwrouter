# `/opt/fwrouter-api/fwrouter_api/services/logs.py`

## Назначение

Централизованная запись и чтение operational/technical logs.

## Важные функции

- `write_operational_log(...)`
- `write_technical_log(...)`
- `list_operational_logs(...)`
- `list_technical_logs(...)`

## Внешние зависимости

- SQLite `operational_logs`
- JSONL files в `/var/log/fwrouter/operational` и `/var/log/fwrouter/technical`
- `core/config.py`

## Runtime/persistent state

- operational events пишутся в SQLite и JSONL
- technical events пишутся в JSONL
- in-memory dedupe state suppress'ит repeated events по `(component,event_type,dedupe_key)`

## Boot persistence relevance

Средняя. Logs не являются dataplane source of truth, но критичны для operator/debug visibility после reboot/apply failures.

## Нюансы

- Dedupe cooldown работает только в рамках текущего backend process.
- Details должны оставаться JSON-serializable и bounded по размеру.

