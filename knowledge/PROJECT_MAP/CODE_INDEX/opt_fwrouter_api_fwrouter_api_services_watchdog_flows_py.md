# `/opt/fwrouter-api/fwrouter_api/services/watchdog_flows.py`

## Назначение

Compatibility facade для watchdog decision flows. Сам manual flow вынесен в `watchdog_manual_flow.py`, automatic scheduler flow вынесен в `watchdog_auto_flow.py`, dependency contract вынесен в `watchdog_flow_deps.py`.

## Важные функции

- `run_vpn_watchdog_check(...)`
  Re-export из `watchdog_manual_flow.py`.

- `run_vpn_watchdog_auto_check(...)`
  Re-export из `watchdog_auto_flow.py`.

- `WatchdogFlowDeps`
  Re-export из `watchdog_flow_deps.py`.

## Runtime/persistent state

- прямых runtime effects нет
- compatibility import path для `watchdog.py`, routes/tests и старых monkeypatch hooks

## Нюансы

- low-level storage/signal/log/scheduler helpers остаются в отдельных watchdog modules
- новая flow-структура: `watchdog_flow_deps.py` -> shared dataclass/constants, `watchdog_manual_flow.py` -> runtime/manual check, `watchdog_auto_flow.py` -> automatic scheduler decision tree
- `watchdog.py` должен оставаться facade-слоем для совместимости API и тестов
