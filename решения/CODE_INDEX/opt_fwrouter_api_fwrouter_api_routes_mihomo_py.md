# `/opt/fwrouter-api/fwrouter_api/routes/mihomo.py`

## Назначение

API для статуса Mihomo, inventory sync, config status/promote/reconcile и runtime restart.

## Важные endpoints

- `GET /api/v2/mihomo`
- `POST /api/v2/mihomo/sync`
- `GET /api/v2/mihomo/config`
  По умолчанию отдаёт lightweight summary без полного YAML payload. Для тяжёлой ручной диагностики доступен query-параметр `include_config=true`.
- `POST /api/v2/mihomo/config/promote`
- `POST /api/v2/mihomo/config/reconcile`
- `GET /api/v2/mihomo/container`
- `POST /api/v2/mihomo/restart`

## Внешние зависимости

- Mihomo services
- Docker runtime validation
- candidate config validation через `docker run ... mihomo -t -f`

## Runtime/persistent state

- может продвигать config и рестартовать контейнер

## Boot persistence relevance

Высокая. Позволяет вручную/программно чинить runtime после boot drift.
