# `/opt/fwrouter-api/fwrouter_api/services/rules_state.py`

## Назначение

Держит persistent state/artifacts/metadata слой для rules subsystem: paths, active/candidate files, rules_state row и rules_metadata rows. После второго разрезания `rules.py` использует этот модуль как storage facade.

## Важные функции

- `get_rules_state()`
- `get_manual_rules_texts()`
- `write_rules_candidate(...)`
- `write_active_rules_state(...)`
- `update_rules_metadata_records(...)`
- `mark_rules_job_running(...)`
- `mark_rules_job_failed(...)`
- `mark_rules_job_success(...)`
- `get_rules_overview()`
- `get_rules_summary()`
- `save_manual_draft(...)`

## Внешние зависимости

- SQLite `rules_state` / `rules_metadata`
- generated rules artifacts under `/var/lib/fwrouter-v2/generated/rules`
- job artifact writers

## Runtime/persistent state

- source of truth для путей/статусов rules subsystem
- управляет snapshot/restore `last-good` artifacts
- пишет metadata про effective/manual/big_* rulesets

## Нюансы

- split сделан без смены import surface: callers всё ещё могут импортировать helpers из `rules.py`
- здесь нет fetch/apply orchestration, только storage/state layer
- `mark_rules_job_running(...)` обязан записывать `last_apply_job_id` / `last_update_job_id` по типу update, иначе UI и диагностика не смогут связать `rules_state.status=running` с job.
- `get_rules_overview()` self-heal'ит старое `running` состояние без активного `apply+rules` job в `failed/RULES_JOB_STALE`; это защищает UI от вечного "обновляется…" после stale job cleanup.
- `get_rules_summary()` возвращает lightweight payload для UI: state, metadata rows, configured sources и manual draft/active text без чтения больших `big-vpn.active.txt` / `effective-rules.json`.
