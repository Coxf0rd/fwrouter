# `/opt/fwrouter-api/fwrouter_api/routes/transfer.py`

## Назначение

API для export/validate/plan/import control-plane snapshots.

## Важные endpoints

- `GET /api/v2/transfer/control-plane/export`
- `GET /api/v2/transfer/control-plane/files`
- `POST /api/v2/transfer/control-plane/validate`
- `POST /api/v2/transfer/control-plane/plan`
- `POST /api/v2/transfer/control-plane/import`

## Внешние зависимости

- `services/control_plane_transfer.py`

## Runtime/persistent state

- export и files list read mostly persistent state
- import может массово изменять persistent control-plane data

## Boot persistence relevance

Средняя. Важен для migration/restore, но не для обычного boot path.
