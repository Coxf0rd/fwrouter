# `/opt/fwrouter-api/fwrouter_api/services/rules_state_selective.py`

## Назначение

Selective-default sync helpers для active effective rules artifacts/state.

## Важные функции

- `effective_rules_with_selective_default(effective_artifact, selective_default)`
- `sync_active_selective_default(selective_default, job_id=None, effective_artifact=None)`

## Нюансы

- Это не full rebuild rules lists; меняет только `selective_default/default_action` в active effective artifacts/state/metadata.
- Сохраняет existing job id только если job реально существует.
