# `/opt/fwrouter-api/fwrouter_api/routes/traffic.py`

## Назначение

API для traffic state, monthly accounting и запуска traffic collection jobs.

## Важные endpoints

- `GET /api/v2/traffic/state`
- `GET /api/v2/traffic/monthly`
- `POST /api/v2/traffic/collect`

## Внешние зависимости

- traffic service
- job manager

## Runtime/persistent state

- collect endpoint создает/запускает `traffic_accounting_collect` job

## Boot persistence relevance

Низкая/средняя. Важен для accounting и watchdog signals.
