# `/opt/fwrouter-api/fwrouter_api/services/watchdog.py`

## Назначение

Фоновый watchdog для VPN runtime и автоматического переключения `vpn-auto` при деградации active server.

## Важные классы

- отдельного класса scheduler нет; используется модульный singleton-подход с thread/event/lock.

## Важные функции

- `detect_recent_vpn_traffic_attempts(...)`
  Читает `traffic_counter_snapshots` и определяет, был ли недавно наблюдаем трафик через VPN path.

- `_has_scoped_vpn_subjects()`
  Проверяет наличие активных `lan`/`tailscale_node` subjects с effective `vpn/selective`. Результат кэшируется коротким TTL, чтобы scheduler не пересчитывал effective state всех клиентов на каждом tick.

- `_recent_successful_active_check(...)`
  Позволяет watchdog reuse'ить свежий успешный `server_ping_state` для active server вместо сетевого delay-check на каждом scheduler tick. Если кэша нет или он устарел, используется обычный `check_active_server_delay(...)`.

- `_watchdog_vpn_auto_state()`
  Коротко кэширует `get_vpn_auto_state()` для scheduler ticks, чтобы watchdog не дергал Mihomo health/selectors на каждом 20-секундном цикле без необходимости.

- `_load_watchdog_module()`
- `_update_watchdog_module(...)`
  Синхронизируют runtime state watchdog в таблице `modules`.

- `run_vpn_watchdog_auto_check(...)`
  Перед failover-решениями читает последний `runtime_convergence` status через read-only API. Если convergence unhealthy, watchdog ставит себя в degraded и не делает failover по потенциально ложному traffic/server signal.

- логика thread lifecycle
  Использует `_WATCHDOG_THREAD`, `_WATCHDOG_STOP_EVENT`, suppression для failure logs.

## Внешние зависимости

- Mihomo adapter
- selector switching
- server ping checks
- routing global state
- DB traffic snapshots
- `runtime_convergence.get_last_runtime_convergence_status`

## Runtime/persistent state

- обновляет module state и пишет operational/technical logs
- не должен чинить dnsmasq/dataplane runtime напрямую; self-heal принадлежит `services/runtime_convergence.py`

## Boot persistence relevance

Средняя. Не нужен для базового boot подъема, но влияет на post-boot runtime stability.

## Нюансы

- watchdog ориентируется на `desired_mode`, а не только на `applied_mode`
- watchdog не должен ограничиваться только global `vpn/selective`: если global mode `direct`, но есть активные `lan`/`tailscale_node` subjects с effective `vpn/selective`, он всё равно использует свежий `path='vpn'` traffic signal и может проверять `vpn-auto`
- watchdog читает runtime convergence как dependency health signal, но не вызывает repair entrypoints
- scheduler tick не должен делать дорогой active-server delay probe чаще, чем истекает short TTL свежего успешного `server_ping_state`; это снижает CPU/network overhead без отключения failover
- selector state тоже кэшируется коротким TTL; если состояние неоднозначно или изменилось после apply, live cache очищается обычными apply/UI paths, а при истечении TTL watchdog снова делает полный health probe
- suppression логов важен, чтобы не зашумлять journald при повторяющихся сбоях
