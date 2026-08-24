# `/opt/fwrouter-api/fwrouter_api/services/rules_state.py`

## Назначение

Compatibility facade для persistent state/artifacts/metadata слоя rules subsystem. Базовый `rules_state` row и path helpers вынесены в `rules_state_store.py`, selective-default sync в `rules_state_selective.py`, active/candidate files в `rules_state_files.py`, metadata/job state в `rules_state_metadata.py`, lightweight read-model в `rules_state_readmodel.py`.

## Важные функции

- `get_rules_state()`
- `get_manual_rules_texts()`
- `write_rules_candidate(...)`
- `write_active_rules_state(...)`
- `update_rules_metadata_records(...)`
- `mark_rules_metadata_update_failed(...)`
- `mark_rules_job_running(...)`
- `mark_rules_job_failed(...)`
- `mark_rules_job_success(...)`
- `get_rules_overview()`
- `get_rules_summary()`
- `save_manual_draft(...)`
- `effective_rules_with_selective_default(...)`
- `sync_active_selective_default(...)`

## Внешние зависимости

- SQLite `rules_state` / `rules_metadata`
- generated rules artifacts under `/var/lib/fwrouter-v2/generated/rules`
- job artifact writers

## Runtime/persistent state

- source of truth для путей/статусов rules subsystem
- управляет snapshot/restore `last-good` artifacts
- пишет metadata про effective/manual/big_* rulesets
- пишет `rules_pipeline_version` в active metadata; это часть безопасной version-only noop проверки для Re-Filter/git-backed rules sources

## Нюансы

- split сделан без смены import surface: callers всё ещё могут импортировать helpers из `rules.py`
- `rules_state.py` сам должен оставаться тонким re-export facade; новые storage/read-model изменения вносить в соответствующий `rules_state_*` модуль
- здесь нет fetch/apply orchestration, только storage/state layer
- `mark_rules_job_running(...)` обязан записывать `last_apply_job_id` / `last_update_job_id` по типу update, иначе UI и диагностика не смогут связать `rules_state.status=running` с job.
- `get_rules_overview()` self-heal'ит старое `running` состояние без активного `apply+rules` job в `failed/RULES_JOB_STALE`; это защищает UI от вечного "обновляется…" после stale job cleanup.
- `get_rules_summary()` возвращает lightweight payload для UI: state, metadata rows, configured sources и manual draft/active text без чтения больших `big-vpn.active.txt` / `effective-rules.json`.
- `mark_rules_job_failed(...)` не должен перетирать `rules_metadata.metadata_json` fallback-кандидатом при failed full-update. Активные counts/version/fetch metadata остаются от last-good rules, а ошибка обновления записывается в `last_error_*`; иначе UI показывает `big_vpn count=0`, хотя active files и dnsmasq продолжают использовать старый Re-filter.
- Смена selective fallback должна синхронно обновлять `rules_state.selective_default`, active `effective-rules.json`, active `effective-rules.txt` и metadata file. Это не rebuild списков, а только смена `selective_default/default_action`, чтобы routing/preflight/profile читали один и тот же default.
