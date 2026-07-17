# `/opt/fwrouter-api/fwrouter_api/services/jobs.py`

## Назначение

Низкоуровневый SQLite-backed слой для хранения и lifecycle management jobs.

## Важные функции

- `create_job(...)`
  Создает queued job и проверяет lock conflicts по `lock_key`.

- `find_active_lock_conflict(lock_key, ...)`
  Ищет активный queued/running job с пересекающимся lock token.

- `cleanup_stale_running_jobs(...)`
  Чистит stale active jobs перед новыми операциями.
  Важно:
  - инвалидирует не только `running`, но и осиротевшие `queued` jobs;
  - иначе после рестарта backend старый queued `apply` job может навсегда держать lock `apply`.

- `mark_job_running(job_id)`
- `touch_job_running(job_id)`
- `update_job_running_result(job_id, ...)`
- `mark_job_success(job_id, ...)`
- `mark_job_failed(job_id, ...)`

## Внешние зависимости

- SQLite `jobs`
- settings (`job_stale_timeout_seconds`, `job_run_now_wait_timeout_seconds`, `job_result_max_bytes`)
- `db_session`

## Runtime/persistent state

- persistent:
  - rows таблицы `jobs`
- runtime:
  - lease semantics через `status` + `updated_at`

## Boot persistence relevance

Средняя. Не dataplane-компонент, но критичен для recoverable apply mutations, UI actions и избежания вечных lock conflicts после restart/crash.

## Нюансы

- lock conflicts считаются по tokenized `lock_key`, а не только по exact string.
- stale queued jobs так же опасны, как stale running jobs: они блокируют новые apply mutations, хотя никакой worker уже не жив.
