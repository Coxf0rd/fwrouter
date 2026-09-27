# `/opt/fwrouter-api/fwrouter_api/services/rules_state_store.py`

## Назначение

Базовый storage слой `rules_state`: path defaults, JSON/text readers, row normalization и upsert single-row state.

## Важные функции

- `_default_rules_paths()`
- `_read_text_if_exists(path)`
- `_read_json_if_exists(path)`
- `_default_rules_state()`
- `_row_to_rules_state(row)`
- `get_rules_state()`
- `_upsert_rules_state_record(state)`
- `_rules_state_with_updates(...)`

## Нюансы

- Модуль не пишет rules artifacts, только row/path helpers.
- The public compatibility path remains through `rules_state.py` and `rules.py`.

## Transaction support (2026-09-27)

`get_rules_state(connection=...)` and `_upsert_rules_state_record(..., connection=...)` support caller-owned SQLite transactions so state and typed audit writes can commit or roll back together.
