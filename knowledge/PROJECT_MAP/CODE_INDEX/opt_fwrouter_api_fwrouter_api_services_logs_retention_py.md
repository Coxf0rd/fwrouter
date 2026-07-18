# `/opt/fwrouter-api/fwrouter_api/services/logs_retention.py`

## Назначение

Retention для JSONL operational/technical log files.

## Важные функции

- `cleanup_log_retention(...)`
- `_cleanup_jsonl_file(...)`

## Внешние зависимости

- `core/config.py`
- `services/artifacts.atomic_write_text`
- JSONL logs в `/var/log/fwrouter`

## Runtime/persistent state

Переписывает log files с удалением строк старше retention cutoff. В `dry_run` только считает.

## Boot persistence relevance

Средняя. Нужен для контроля роста диска, но не влияет на live dataplane.

## Нюансы

- Invalid JSON/timestamp lines сохраняются, а не удаляются silently.
- Retention не должен удалять SQLite operational logs; это делает `maintenance.py`.

