# `/opt/fwrouter-api/fwrouter_api/services/jobs_retention.py`

## Назначение

Очищает старые rows из `jobs` и соответствующие artifact directories в `/var/lib/fwrouter-v2/jobs`.

## Важные функции

- `cleanup_jobs_retention(...)`
  Удаляет завершенные jobs по age/count policy, защищая `queued/running`.

## Runtime/persistent state

- SQLite `jobs`
- `/var/lib/fwrouter-v2/jobs/<job_id>`
- ссылки `rules_state.last_apply_job_id/last_update_job_id`

## Нюансы

- retention теперь учитывает не только age, но и max count по `job_type/status`, чтобы частые success jobs не копились бесконечно.
- `apply_mutation` success/failed jobs имеют отдельные более узкие лимиты, потому что их dataplane artifacts самые тяжелые.
- cleanup сбрасывает ссылки из `rules_state` перед удалением job rows.
- dry-run отчет включает artifact bytes для candidates и orphan dirs.
