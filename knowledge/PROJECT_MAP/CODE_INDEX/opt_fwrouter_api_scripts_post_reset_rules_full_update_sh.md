# `/opt/fwrouter-api/scripts/post-reset-rules-full-update.sh`

## Назначение

Post-reset helper для запуска `/rules/full-update` и проверки effective rules/runtime.

## Важные функции

- health check
- `POST /rules/full-update`
- `GET /rules/effective`
- `GET /runtime`

## Внешние зависимости

- `curl`
- backend API

## Runtime/persistent state

Не read-only: скачивает/валидирует/promote-ит rules artifacts через backend job path.

## Boot persistence relevance

Средняя. Полезен после reset/rebuild, чтобы восстановить effective rules state.

## Нюансы

- Использует env `API_BASE_URL` и `REQUESTED_BY`.

