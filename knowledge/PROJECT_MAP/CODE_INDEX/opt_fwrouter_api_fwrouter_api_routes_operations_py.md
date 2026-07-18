# `/opt/fwrouter-api/fwrouter_api/routes/operations.py`

## Назначение

API для operational jobs: dry-run apply, maintenance cleanup и full refresh.

## Важные endpoints

- `POST /api/v2/apply/dry-run`
- `POST /api/v2/maintenance/cleanup`
- `POST /api/v2/full-refresh`

## Внешние зависимости

- job manager
- full refresh service
- job conflict handling

## Runtime/persistent state

- может создавать и запускать jobs, а `full-refresh` меняет runtime/persistent artifacts

## Boot persistence relevance

Средняя. Полезен для safe post-boot verification and repair workflows.
