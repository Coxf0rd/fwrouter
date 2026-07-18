# `/opt/fwrouter-api/scripts/post-clean-reset-bootstrap.sh`

## Назначение

Post-reset bootstrap helper: проверяет API, sync inventory и базовые runtime endpoints после clean reset/rebuild.

## Важные функции

- health check
- subject/system inventory sync
- optional Docker/host/Tailscale/Xray discovery flags
- выводит JSON responses в readable form

## Внешние зависимости

- backend API
- `/opt/fwrouter-api/.venv/bin/python`

## Runtime/persistent state

Может создавать/обновлять subjects/system subjects через sync endpoints.

## Boot persistence relevance

Средняя/высокая для post-rebuild процедуры.

## Нюансы

- `DISCOVER_TAILSCALE` и `INCLUDE_ALL_TAILSCALE_PEERS` должны быть выставлены осознанно, чтобы не засорить subjects нерouted peers.

