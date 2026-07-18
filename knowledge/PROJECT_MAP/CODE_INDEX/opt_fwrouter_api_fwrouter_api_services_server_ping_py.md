# `/opt/fwrouter-api/fwrouter_api/services/server_ping.py`

## Назначение

Измеряет задержку серверов через Mihomo adapter и обновляет `server_ping_state`.

## Важные функции

- `check_server_delay(server_id, ...)`
  Для custom proxy принимает persistent `server_id`, но проверяет Mihomo по runtime target `server_name` (`Proxy6`), сохраняя `server_ping_state` обратно по persistent id.
- `check_active_server_delay(...)`
- `check_server_delay_sweep(...)`

## Внешние зависимости

- Mihomo adapter delay checks
- DB

## Runtime/persistent state

- при `update_state=True` пишет `server_ping_state`
- custom proxy имеет разные identifiers: DB/API `server_id` вида `custom-https:*`, Mihomo target name вида `Proxy6`

## Boot persistence relevance

Низкая/средняя. Важен для selector/watchdog/ops, но не для базового boot path.
