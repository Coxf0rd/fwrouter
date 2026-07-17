# `/opt/fwrouter-api/fwrouter_api/routes/runtime.py`

## Назначение

Read-only runtime inspection endpoints.

## Важные endpoints

- `GET /api/v2/runtime`
- `GET /api/v2/runtime/scoped-egress`

## Внешние зависимости

- `services/runtime.py`
- scoped egress runtime summary

## Runtime/persistent state

- read-only

## Boot persistence relevance

Высокая как основной inspection API после reboot.
