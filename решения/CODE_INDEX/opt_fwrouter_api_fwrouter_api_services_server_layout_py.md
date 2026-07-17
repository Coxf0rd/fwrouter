# `/opt/fwrouter-api/fwrouter_api/services/server_layout.py`

## Назначение

Возвращает expected server-root layout FWRouter.

## Важные функции

- `get_server_root_layout()`

## Внешние зависимости

- `core/config.py`

## Runtime/persistent state

State не пишет. Читает settings paths и строит diagnostic layout object.

## Boot persistence relevance

Средняя. Используется для diagnostics/transfer/install sanity, но не применяет runtime state.

## Нюансы

- `expected_units` должен обновляться при добавлении systemd units/timers.
- `SERVER_LAYOUT_CONTRACT_VERSION` должен меняться при значимом изменении layout expectations.

