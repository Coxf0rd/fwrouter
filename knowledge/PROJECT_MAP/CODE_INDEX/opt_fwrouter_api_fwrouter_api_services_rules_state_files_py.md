# `/opt/fwrouter-api/fwrouter_api/services/rules_state_files.py`

## Назначение

File/artifact storage layer для active/candidate rules files и last-good snapshots.

## Важные функции

- `get_manual_rules_texts()`
- `_build_metadata_file(...)`
- `restore_last_good_rules()`
- `write_rules_candidate(...)`
- `write_active_rules_state(...)`

## Нюансы

- Пишет generated candidate/active artifacts через shared atomic writers.
- Перед promotion active state snapshot-ит last-good rules files.
