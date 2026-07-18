# `/opt/fwrouter-api/fwrouter_api/jobs/manager.py`

## Назначение

Легковесный SQLite-backed job manager с background threads и явной регистрацией safe handlers.

## Важные классы

- `JobManager`
  Ответственность:
  - регистрация handlers
  - создание job rows
  - запуск фоновых worker threads
  - bounded wait
  - cleanup stale active jobs

## Важные методы

- `register_handler(job_type, handler)`
- `create(...)`
- `start_job(job_id)`
- `wait_for_job(job_id, ...)`
- `wait_for_idle(...)`
  Ждет завершения уже стартованных worker threads. Используется тестами/интеграционными сценариями перед сменой isolated DB/env, чтобы не получить SQLite lock от хвоста предыдущего job worker.
- `start_job_and_wait(job_id, ...)`
- `run_job(job_id)`

## Внешние зависимости

- settings
- jobs service functions
- technical logs
- handler registry из `jobs.handlers`

## Runtime/persistent state

- пишет/обновляет jobs в SQLite
- держит in-memory map worker threads

## Boot persistence relevance

Средняя. Не основной boot path, но важен для apply/rules pipelines и scheduler-driven operations.

## Нюансы

- manager сам не должен знать arbitrary runtime behavior; только registered handlers
- `start_job_and_wait(...)` использует bounded synchronous wait для API `run_now` paths; timeout должен быть выше типичной длительности `apply_mutation`, иначе UI может увидеть request-level error/timeout при уже успешно завершившемся background apply
- stale job cleanup нужен, чтобы reboot/backend crash не блокировал новые операции
- stale cleanup опирается на `services/jobs.py`; orphaned `queued` jobs тоже должны сниматься, иначе lock `apply` может зависнуть навсегда
