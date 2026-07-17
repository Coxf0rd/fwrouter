# `/opt/fwrouter-api/fwrouter_api/services/full_refresh.py`

## Назначение

Сквозной orchestration pipeline для полной operational resync: system subjects, subject inventory, xray subject sync, rules full update, subscription refresh.

## Важные функции

- `_run_subject_inventory_sync(...)`
- `run_full_refresh(requested_by=...)`

## Внешние зависимости

- job manager
- system_subjects service
- xray sync
- rules full update
- subscription refresh

## Runtime/persistent state

- запускает цепочку jobs и apply-like refresh operations

## Boot persistence relevance

Средняя. Не часть boot path, но полезен как post-boot repair/resync workflow.
