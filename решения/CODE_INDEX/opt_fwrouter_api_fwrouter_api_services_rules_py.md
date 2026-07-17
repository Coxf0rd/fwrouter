# `/opt/fwrouter-api/fwrouter_api/services/rules.py`

## Назначение

Канонический rules facade: normalization/validation, effective rules compilation и compatibility entrypoints. После разрезания storage/state helpers вынесены в `rules_state.py`, artifact workflows в `rules_artifacts.py`, job workflows в `rules_jobs.py`, но публичный import surface оставлен здесь.

## Важные функции

- `validate_manual_rules(...)`
- `validate_value_list(...)`
- `build_effective_rules_artifact(...)`
- `render_effective_rules_text(...)`
- `get_manual_rules_texts()`
- `get_rules_overview()`
- `get_rules_summary()`
- `prepare_manual_rules_candidate(...)`
- `finalize_manual_rules_apply(...)`
- `run_rules_full_update(...)`

Последние функции теперь mostly thin wrappers в отдельные workflow/storage модули.

## Внешние зависимости

- rules source adapter
- apply pipeline
- SQLite state/metadata
- generated rules artifacts

## Runtime/persistent state

- ведет active/effective rules artifacts
- обновляет metadata/state rows
- отдает стабильный import surface для routes/tests/other services

## Нюансы

- split сделан без смены API: тесты и сервисы продолжают импортировать symbols из `rules.py`
- state/metadata persistence cluster уже вынесен в `rules_state.py`; если резать дальше, следующий кандидат это validation/compile cluster
- `validate_value_list(...)` для external `big_vpn` пропускает protected local/service networks (`100.64.0.0/10`, RFC1918 и т.п.) и считает их в `compile_stats.protected_vpn_skipped`; одна такая строка в upstream Re-filter не должна валить весь `/rules/full-update`, потому что protected rules и так имеют приоритет выше `big_vpn`.
