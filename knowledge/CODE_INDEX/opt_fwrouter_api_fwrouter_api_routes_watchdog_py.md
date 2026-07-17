# `/opt/fwrouter-api/fwrouter_api/routes/watchdog.py`

## Назначение

API для controlled/automatic watchdog checks и подтвержденного selector switch.

## Важные endpoints

- `POST /api/v2/watchdog/vpn/check`
- `POST /api/v2/watchdog/vpn/auto-check`

## Внешние зависимости

- watchdog service

## Runtime/persistent state

- может косвенно переключать Mihomo selector и обновлять ping state

## Boot persistence relevance

Средняя. Не нужен для самого boot, но важен для post-boot resiliency.
