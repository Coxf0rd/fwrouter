# `/opt/fwrouter-api/fwrouter_api/core/paths.py`

## Назначение

Каноническая карта filesystem layout FWRouter.

## Важные функции

- `FWRouterPaths`
  Dataclass с базовыми путями `/etc/fwrouter`, `/var/lib/fwrouter-v2`, `/var/log/fwrouter`, `/run/fwrouter-v2`.
- derived properties:
  - `db_path`
  - `rules_dir`
  - `generated_dir`
  - `jobs_dir`
  - `cache_dir`
  - `runtime_state_dir`
  - `operational_log_dir`
  - `technical_log_dir`
  - `operational_events_path`
- `DEFAULT_PATHS`

## Внешние зависимости

Нет.

## Runtime/persistent state

Не пишет state; задает пути, куда другие модули пишут persistent/runtime artifacts.

## Boot persistence relevance

Высокая. Изменение layout ломает install, boot preflight, SQLite path, generated artifacts, jobs и logs.

## Нюансы

- Не менять пути без синхронного обновления install scripts, systemd units, troubleshooting и docs.

