# `/opt/fwrouter-api/fwrouter_api/jobs/base.py`

## Назначение

Базовые status/time helpers для persistent job model.

## Важные функции

- `JobStatus`
  Enum `queued`, `running`, `success`, `failed`, `cancelled`.
- `utc_now()`
  UTC timestamp helper.
## Внешние зависимости

Нет внешних сервисов.

## Runtime/persistent state

Сам файл state не пишет. Persistent job rows живут в `services/jobs.py` и `jobs/manager.py`.

## Boot persistence relevance

Средняя. Enum/status vocabulary должен оставаться совместимым с таблицей `jobs` и UI/API consumers.
