# `/opt/fwrouter-api/fwrouter_api/services/rules_state_metadata.py`

## Назначение

`rules_metadata` rows и job status helpers для rules subsystem.

## Важные функции

- `list_rules_metadata()`
- `_upsert_ruleset_metadata(...)`
- `update_rules_metadata_records(...)`
- `mark_rules_metadata_update_failed(...)`
- `mark_rules_job_running(...)`
- `mark_rules_job_failed(...)`
- `mark_rules_job_success(...)`

## Нюансы

- Failed full-update не должен затирать last-good metadata counts.
- `mark_rules_job_running(...)` обязан сохранять `last_apply_job_id` / `last_update_job_id` по update type.
- `_repair_stale_running_rules_state(...)` repairs stale running state without an active `apply+rules` job.

## Audit behavior (2026-09-27)

Successful changed manual active-set commits can include `rules.manual_set_activated` in the same SQLite transaction as the successful rules-state update. The audit payload contains hashes/counts only; automatic full updates do not emit this admin event.
