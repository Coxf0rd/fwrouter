# `/opt/fwrouter-api/fwrouter_api/routes/system_subjects.py`

## Назначение

API для system subjects (`docker`, `host`, `fwrouter`) и их sync/tombstone lifecycle.

## Важные endpoints

- `GET /api/v2/system-subjects`
- `GET /api/v2/system-subjects/{subject_id}`
- `POST /api/v2/system-subjects/{subject_id}/mode`
- `POST /api/v2/system-subjects/sync`
- `DELETE /api/v2/system-subjects/{subject_id}`

## Внешние зависимости

- system_subjects service
- apply mutation

## Runtime/persistent state

- sync и delete меняют persistent subject state

## Boot persistence relevance

Средняя/высокая. Builtin/system subjects должны оставаться консистентными после reboot.
