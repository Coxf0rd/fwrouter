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
- Публичный compatibility path остается через `rules_state.py` и `rules.py`.
