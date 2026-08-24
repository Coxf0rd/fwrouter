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
- `_repair_stale_running_rules_state(...)` чинит stale running без active `apply+rules` job.
