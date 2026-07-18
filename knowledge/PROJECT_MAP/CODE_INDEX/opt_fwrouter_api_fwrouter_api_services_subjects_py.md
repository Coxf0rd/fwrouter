# `/opt/fwrouter-api/fwrouter_api/services/subjects.py`

## Назначение

Базовый read/write слой для subjects и их detail-таблиц.

## Важные функции

- `list_subjects(...)`
  Возвращает subjects из SQLite. При `include_detail=True` detail rows грузятся bulk-запросами по detail table, а не отдельным `get_subject_detail()` на каждый subject; это важно для subject-mode apply path, который строит manifest по всем subjects.

- `canonical_subject_type(...)`
- `get_subject_detail(subject_id, subject_type)`
- `get_subject(subject_id)`
- `find_subject_by_ip(ip_address)`
  Используется user-facing `/ui/whoami`. Делает прямой SQL lookup по `subject_lan.ip_address` и `subject_tailscale.tailscale_ip` только среди active/non-deleted subjects, чтобы user view не обходил весь inventory.
- `update_subject_alias(subject_id, alias)`
- `list_subjects(...)`

## Внешние зависимости

- DB

## Runtime/persistent state

- alias updates меняют persistent subject state

## Boot persistence relevance

Высокая. Это фундаментальный read model для subject-driven routing semantics.
