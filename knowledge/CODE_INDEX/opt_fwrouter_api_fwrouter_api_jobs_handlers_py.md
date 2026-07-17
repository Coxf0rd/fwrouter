# `/opt/fwrouter-api/fwrouter_api/jobs/handlers.py`

## Назначение

Регистрирует built-in safe job handlers первого слоя.

## Важные функции

- `noop_handler(job)`
- `runtime_probe_handler(job)`
- `apply_dry_run_handler(job)`
- `subscription_refresh_prepare_handler(job)`
- `jobs_retention_cleanup_handler(job)`
- `server_ping_sweep_handler(job)`
- `apply_mutation_handler(job)`

- `register_default_handlers(manager)`
  Подключает эти handlers к `JobManager`.

## Внешние зависимости

- apply pipeline
- runtime summary
- subscription pipeline
- jobs retention
- server ping
- apply orchestrator

## Runtime/persistent state

- через handlers может создавать job artifacts и mutation results

## Boot persistence relevance

Средняя. Важен для mutation/job API, но не для базового boot path.

## Нюансы

- здесь важна redaction логика subscription URLs и metadata
