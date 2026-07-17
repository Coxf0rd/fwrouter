# `/opt/fwrouter-api/fwrouter_api/services/action_contract.py`

## Назначение

Helpers для единообразных API responses вокруг mutation/job actions.

## Важные функции

- `build_job_action_response(...)`
- `build_conflict_response(...)`

## Внешние зависимости

- `schemas.ApiResponse`

## Runtime/persistent state

State не читает и не пишет.

## Boot persistence relevance

Низкая для boot, средняя для UX/API consistency.

## Нюансы

- Использовать для mutation routes, чтобы `JOB_CONFLICT` и job payload выглядели одинаково в UI.

