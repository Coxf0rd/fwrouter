# `/opt/fwrouter-api/fwrouter_api/services/core_bypass.py`

## Назначение

Управляет FWRouter core bypass: временно выключает traffic core/dataplane-dependent runtime и возвращает систему в direct-safe состояние.

## Важные функции

- `get_core_bypass_state()`
- `is_core_bypass_enabled()`
- `enable_core_bypass(...)`
- `disable_core_bypass(...)`
- `core_bypass_handler(...)`
- `submit_core_bypass_job(...)`

## Внешние зависимости

- SQLite `settings` key `core.bypass`
- `modules`
- `servers.ensure_routing_global_state`
- `subjects.list_subjects`
- `jobs.manager`
- `logs.write_operational_log`
- `subprocess` для live bypass/apply helpers

## Runtime/persistent state

- persistent state хранится в `settings.value_json` под key `core.bypass`
- runtime modules `vpn`, `xray`, `watchdog`, `selector` получают bypass runtime state
- clearing live probe cache обязателен после state changes

## Boot persistence relevance

Высокая. Bypass state должен переживать backend restart и не оставлять систему в полупримененном VPN/dataplane состоянии.

## Нюансы

- Lock key `apply+module:core+selector+xray` намеренно широкий: bypass конфликтует с обычными apply/module transitions.
- Включение bypass должно сохранить `previous_runtime`, чтобы disable мог восстановить dependent modules.
- Bypass не означает выключение UI/API; это режим обхода traffic core.

