# `/opt/fwrouter-api/fwrouter_api/routes/jobs.py`

## Назначение

Generic Jobs API с жестко ограниченным allowlist разрешенных job types.

## Важные endpoints

- `GET /api/v2/jobs`
- `POST /api/v2/jobs`
- `GET /api/v2/jobs/{job_id}`
- `POST /api/v2/jobs/{job_id}/run`

## Внешние зависимости

- `JobManager`
- jobs service
- action contract for conflicts

## Runtime/persistent state

- создает и запускает jobs rows в SQLite

## Boot persistence relevance

Средняя. Позволяет безопасно запускать диагностические jobs после boot.

## Нюансы

- API intentionally blocks dangerous job types и non-dry-run сценарии для некоторых handlers
