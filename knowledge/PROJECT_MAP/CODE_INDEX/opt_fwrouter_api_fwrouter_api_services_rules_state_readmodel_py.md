# `/opt/fwrouter-api/fwrouter_api/services/rules_state_readmodel.py`

## Назначение

Lightweight read-model для rules UI/API.

## Важные функции

- `get_rules_overview()`
- `get_rules_summary()`
- `save_manual_draft(text)`
- `get_effective_rules()`

## Нюансы

- `get_rules_summary()` не читает большие active big-vpn/effective JSON payloads без необходимости.
- `save_manual_draft()` пишет draft и возвращает overview с validation.
