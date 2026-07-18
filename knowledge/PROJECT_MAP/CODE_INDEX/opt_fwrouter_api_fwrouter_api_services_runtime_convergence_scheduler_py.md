# `/opt/fwrouter-api/fwrouter_api/services/runtime_convergence_scheduler.py`

## Назначение

In-process scheduler для периодического runtime convergence check selective/VPN path.

## Важные функции

- `start_runtime_convergence_scheduler()`
- `stop_runtime_convergence_scheduler(...)`
- `_runtime_convergence_scheduler_loop()`

## Внешние зависимости

- `core/config.py`
- `services/runtime_convergence.run_runtime_convergence_check`
- `services/logs.write_technical_log`

## Runtime/persistent state

Держит daemon thread внутри backend process. Сам persistent state не хранит.

## Boot persistence relevance

Средняя. Дополняет startup recovery и ежедневный maintenance, чтобы broken dnsmasq nftset/runtime drift не ждали следующего systemd timer.

## Нюансы

- Управляется `runtime_convergence_scheduler_enabled` и `runtime_convergence_interval_seconds`.
- Ошибки tick логируются как technical warning.
- Не должен запускаться повторно, если thread уже жив.
