# `/opt/fwrouter-api/fwrouter_api/services/maintenance_scheduler.py`

## Назначение

In-process background scheduler для periodic control-plane maintenance.

## Важные функции

- `start_maintenance_scheduler()`
- `stop_maintenance_scheduler(...)`
- `_maintenance_scheduler_loop()`

## Внешние зависимости

- `core/config.py`
- `services/maintenance.run_control_plane_maintenance`
- `services/logs.write_technical_log`

## Runtime/persistent state

Держит daemon thread внутри backend process. Сам state не хранит, но запускает real maintenance с `dry_run=False`.

## Boot persistence relevance

Средняя. Дополняет systemd timer; ошибки scheduler не должны валить backend.

## Нюансы

- Управляется `maintenance_scheduler_enabled` и `maintenance_interval_seconds`.
- Ошибки tick логируются как technical warning.
- Не должен запускаться повторно, если thread уже жив.

