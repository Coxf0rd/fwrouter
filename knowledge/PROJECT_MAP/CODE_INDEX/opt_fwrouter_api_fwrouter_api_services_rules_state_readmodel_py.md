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
- `save_manual_draft()` writes draft and returns overview with validation.

## Audit behavior (2026-09-27)

Changed drafts write an atomic SQLite audit row with caller attribution and hash/count-only before/after values. Identical text returns current validation/overview without rewriting the file, changing rules state, or emitting an audit event.
