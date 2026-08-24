# `/opt/fwrouter-api/fwrouter_api/services/watchdog_flow_deps.py`

## Назначение

Shared constants и `WatchdogFlowDeps` dependency contract для watchdog manual/automatic flows.

## Важные элементы

- `DEFAULT_WATCHDOG_TIMEOUT_MS`
- `DEFAULT_WATCHDOG_CANDIDATE_LIMIT`
- `WATCHDOG_RUNTIME_RUNNING`
- `WATCHDOG_RUNTIME_PAUSED`
- `WATCHDOG_RUNTIME_DEGRADED`
- `WatchdogFlowDeps`
  Dataclass callbacks, через который facade `watchdog.py` передает runtime controller, traffic signal, module state, logs, cooldown/failure state и routing helpers.

## Нюансы

- Модуль не должен импортировать flow modules, чтобы не создавать cycle.
- Callback contract сохраняет старые monkeypatch/test hooks на facade уровне.
