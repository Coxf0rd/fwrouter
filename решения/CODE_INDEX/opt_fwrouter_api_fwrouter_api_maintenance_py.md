# `/opt/fwrouter-api/fwrouter_api_maintenance.py`

## Назначение

CLI/module entrypoint для control-plane maintenance.

## Важные функции

- `main()` / module execution path
  Запускает `run_control_plane_maintenance(dry_run=False)` и печатает/логирует результат.

## Внешние зависимости

- `services/maintenance.py`
- systemd `fwrouter-maintenance.service`

## Runtime/persistent state

Запускает real cleanup/retention/compaction path. Может удалять старые artifacts/logs/rows согласно conservative retention policy.

## Boot persistence relevance

Средняя/высокая. Нужен для контроля роста SQLite/filesystem, но cleanup должен оставаться безопасным для last-good/current artifacts.

## Нюансы

- Для ручной проверки сначала использовать API/job или Python dry-run `run_control_plane_maintenance(dry_run=True)`.

