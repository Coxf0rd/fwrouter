# `/opt/fwrouter-api/fwrouter_api/services/mihomo_runtime.py`

## Назначение

Управляет operational restart/status для Mihomo контейнера через `docker compose`.

## Важные функции

- `_run_compose_command(args, timeout_seconds=...)`
  Общая обертка для compose-команд.

- `get_mihomo_container_status()`
  Read-only Docker status check.

- `wait_for_mihomo_controller(...)`
  Ждет доступности controller через adapter health.

- `restart_mihomo_container(...)`
  Controlled restart с post-check и optional heartbeat callback.

## Внешние зависимости

- Docker Compose file `/opt/fwrouter-mihomo/docker-compose.yml`
- Mihomo adapter health

## Runtime/persistent state

- меняет только container runtime state

## Boot persistence relevance

Средняя. Boot старт делает systemd unit, но runtime reconcile paths используют этот модуль.

## Нюансы

- restart считается успешным только если одновременно успешны compose, `ps` и controller readiness
