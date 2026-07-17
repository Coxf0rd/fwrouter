# `/opt/fwrouter-api/fwrouter_api/routes/core.py`

## Назначение

API routes для FWRouter core bypass.

## Важные endpoints

- `GET /api/v2/core/bypass`
  Возвращает current bypass state и доступные actions.
- `POST /api/v2/core/bypass/enable`
  Включает bypass через job. Требует `confirm_apply=true`.
- `POST /api/v2/core/bypass/disable`
  Выключает bypass через job. Требует `confirm_apply=true`.

## Внешние зависимости

- `services/core_bypass.py`
- `schemas.ApiResponse`

## Runtime/persistent state

Меняет state только через core bypass job. Сам route handler не применяет dataplane напрямую.

## Boot persistence relevance

Высокая. Core bypass переводит runtime в direct-safe/bypass состояние и влияет на dependent modules.

## Нюансы

- Отсутствие `confirm_apply=true` возвращает `CORE_BYPASS_CONFIRMATION_REQUIRED`.
- Job lock conflict возвращает `JOB_CONFLICT` с active job.

