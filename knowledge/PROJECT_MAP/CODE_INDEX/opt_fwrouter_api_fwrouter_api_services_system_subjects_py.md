# `/opt/fwrouter-api/fwrouter_api/services/system_subjects.py`

## Назначение

Управляет builtin system subjects, их enrichment, sync-request path и tombstone semantics.

## Важные функции

- `ensure_builtin_system_subjects()`
  Гарантирует наличие `fwrouter:global` и builtin management subjects. Если в БД остался неканонический active row с тем же `(subject_type, stable_key)`, он tombstone'ится перед созданием canonical builtin subject.

- `list_system_subjects(...)`
- `get_system_subject(subject_id)`
- `delete_system_subject(subject_id, ...)`
- `request_system_subject_sync(...)`

## Внешние зависимости

- DB
- subject policy/services
- job manager

## Runtime/persistent state

- может создавать builtin rows и tombstone system subjects

## Boot persistence relevance

Высокая для builtin control-plane/system subject consistency.

## Нюансы

- `fwrouter:global` нельзя удалять
- при upsert `fwrouter:global` очищаются subject server overrides
- `fwrouter:global` принудительно нормализуется в `desired_mode=direct` и `applied_mode=direct`; оставлять builtin subject в `vpn/selective` нельзя
- bootstrap должен быть идемпотентен на старых DB: unique conflict по `(subject_type, stable_key)` не должен ронять startup
