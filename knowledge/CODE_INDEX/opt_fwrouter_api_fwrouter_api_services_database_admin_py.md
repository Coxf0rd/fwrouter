# `/opt/fwrouter-api/fwrouter_api/services/database_admin.py`

## Назначение

Admin helpers для schema state, backup, rebuild и post-rebuild reconcile control-plane DB.

## Важные функции

- `get_database_schema_state()`
- `backup_database_file()`
- `reconcile_control_plane_runtime(...)`
- `rebuild_control_plane_database(...)`

## Внешние зависимости

- `db/connection.py`
- `db/schema_state.py`
- `services/control_plane_transfer.py`
- `services/modules.py`
- `services/system_subjects.py`
- `services/subject_inventory.py`
- `services/logs.py`

## Runtime/persistent state

- может копировать `/var/lib/fwrouter-v2/fwrouter.db` в backups
- rebuild может заменить/импортировать control-plane snapshot
- reconcile syncs builtin system subjects и inventory

## Boot persistence relevance

Высокая. Ошибка rebuild/reconcile может повредить source-of-truth DB.

## Нюансы

- Перед rebuild делать backup.
- После import нужен runtime reconcile, иначе SQLite state и discovered system/subjects могут разойтись.

