# `/opt/fwrouter-api/fwrouter_api/services/artifacts.py`

## Назначение

Безопасная запись JSON/text/file artifacts и job artifact helpers.

## Важные функции

- `atomic_write_text(...)`
- `atomic_write_json(...)`
- `atomic_copy_file(...)`
- `get_job_artifact_dir(...)`
- `ensure_job_artifact_dir(...)`
- `write_job_json_artifact(...)`
- `write_job_text_artifact(...)`

## Внешние зависимости

- `core/config.py`
- filesystem `/var/lib/fwrouter-v2/jobs`

## Runtime/persistent state

Пишет artifacts atomically через temp file + fsync + replace.

## Boot persistence relevance

Высокая для apply/debug safety. Поврежденный artifact может сломать rollback/debug или усложнить восстановление.

## Нюансы

- Не заменять atomic helpers простым write, если artifact используется apply/rollback/retention.
- Path validation в job artifact names защищает от выхода за job directory.
