# `/opt/fwrouter-api/fwrouter_api/routes/server_ping.py`

## Назначение

API для active server ping check и sweep по множеству серверов.

## Важные endpoints

- `GET/POST /api/v2/server-ping/active`
- `GET/POST /api/v2/server-ping/sweep`

## Внешние зависимости

- server_ping service

## Runtime/persistent state

- POST endpoints могут обновлять `server_ping_state`

## Boot persistence relevance

Низкая/средняя. Используется selector/watchdog/runtime ops, но не обязателен для самого boot.
