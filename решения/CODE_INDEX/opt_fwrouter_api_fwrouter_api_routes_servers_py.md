# `/opt/fwrouter-api/fwrouter_api/routes/servers.py`

## Назначение

API для server inventory, preferences, custom proxy servers и global routing mutations.

## Важные endpoints

- `GET /api/v2/servers`
- `POST /api/v2/servers/sync/mihomo`
- `PATCH /api/v2/servers/{server_id}/preferences`
- `PUT /api/v2/servers/vpn-auto`
- `POST /api/v2/routing/global`
- `POST /api/v2/routing/global/fixed-server`
- `DELETE /api/v2/routing/global/fixed-server`
- subject server override endpoints

## Внешние зависимости

- `services/servers.py`
- custom servers service
- apply orchestrator/job action contract
- runtime enforcement state

## Runtime/persistent state

- читает и меняет routing global state, server preferences и override state

## Boot persistence relevance

Критическая. Через этот API меняется persisted routing intent, который должен пережить reboot.

## Нюансы

- часть endpoints запускает asynchronous apply jobs
- нельзя ломать conflict handling через `JobLockConflictError`
