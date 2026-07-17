# `/opt/fwrouter-api/fwrouter_api/services/modules.py`

## Назначение

Управляет lifecycle и desired state модулей control-plane.

## Важные функции

- `fetch_modules()`
- `find_module(...)`
- `get_module_state(module_name)`
- `set_module_desired_state(...)`
- `run_module_action(...)`

## Внешние зависимости

- DB
- job manager
- tailscale runtime/actions
- subscription refresh prepare

## Runtime/persistent state

- обновляет таблицу `modules`
- может создавать связанные jobs

## Boot persistence relevance

Средняя/высокая. Module desired/runtime states участвуют в readiness и scheduler behavior.

## Нюансы

- `watchdog` runtime state может быть overridden config-ом, даже если DB говорит иное
