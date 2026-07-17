# `/opt/fwrouter-api/fwrouter_api/routes/modules.py`

## Назначение

API для module desired state и module actions.

## Важные endpoints

- `GET /api/v2/modules`
- `GET /api/v2/modules/{module_name}`
- `POST /api/v2/modules/{module_name}/desired-state`
- `POST /api/v2/modules/{module_name}/actions/{action}`

## Внешние зависимости

- modules service

## Runtime/persistent state

- меняет `modules` table и может запускать follow-up jobs/actions

## Boot persistence relevance

Средняя/высокая. Module desired state участвует в startup/runtime behavior.
