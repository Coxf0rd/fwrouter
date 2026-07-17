# `/usr/local/sbin/fwrouter-jobs-retention-dry-run`

## Назначение

Systemd wrapper для ежедневного dry-run jobs retention cleanup.

## Важные функции

- строит payload `job_type=jobs_retention_cleanup`
- использует lock `jobs_retention`
- выставляет `input_data.dry_run=true`
- проверяет, что dry-run ничего не удалил

## Внешние зависимости

- backend API `http://127.0.0.1:5000/api/v2/jobs`
- `fwrouter-jobs-retention-dry-run.service`
- `fwrouter-jobs-retention-dry-run.timer`

## Runtime/persistent state

Не должен удалять state. Это diagnostic guard вокруг retention path.

## Boot persistence relevance

Низкая/средняя. Не нужен для materialization dataplane, но помогает заранее увидеть retention anomalies.

## Нюансы

- Любой `deleted_jobs_count` или `deleted_artifact_dirs_count` в dry-run считается ошибкой wrapper-а.
- Real cleanup выполняется maintenance path, не этим timer-ом.

