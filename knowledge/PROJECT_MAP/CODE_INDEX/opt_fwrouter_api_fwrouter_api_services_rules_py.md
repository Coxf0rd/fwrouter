# `/opt/fwrouter-api/fwrouter_api/services/rules.py`

## Назначение

Канонический rules compatibility facade. Normalization/validation/effective rules compilation вынесены в `rules_compile.py`, storage/state helpers вынесены в `rules_state.py`, artifact workflows в `rules_artifacts.py`, job workflows в `rules_jobs.py`, но публичный import surface оставлен здесь.

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

Эти функции теперь mostly thin wrappers или re-export entrypoints в отдельные compile/workflow/storage модули.

## Внешние зависимости

- rules source adapter
- `rules_compile.py` для constants, source-policy classification, validation, compiler и text renderer
- apply pipeline
- SQLite state/metadata
- generated rules artifacts

## Runtime/persistent state

- ведет active/effective rules artifacts
- обновляет metadata/state rows
- отдает стабильный import surface для routes/tests/other services

## Нюансы

- split сделан без смены API: тесты и сервисы продолжают импортировать symbols из `rules.py`
- `rules.py` должен оставаться тонким фасадом, потому что `rules_state.py` и `rules_jobs.py` исторически обращаются к нему как к `rules_service.*`
- state/metadata persistence cluster уже вынесен в `rules_state.py`; validation/compile cluster вынесен в `rules_compile.py`
- `effective_rules_with_selective_default(...)` / `sync_active_selective_default(...)` меняют только selective fallback в active artifacts/state; они не пересобирают и не инвертируют списки DIRECT/VPN.
- `validate_value_list(...)` для external `big_vpn` пропускает protected local/service networks (`100.64.0.0/10`, RFC1918 и т.п.) и считает их в `compile_stats.protected_vpn_skipped`; одна такая строка в upstream Re-filter не должна валить весь `/rules/full-update`, потому что protected rules и так имеют приоритет выше `big_vpn`.
