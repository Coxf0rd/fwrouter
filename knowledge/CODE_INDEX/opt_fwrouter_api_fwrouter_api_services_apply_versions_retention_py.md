# `/opt/fwrouter-api/fwrouter_api/services/apply_versions_retention.py`

## Назначение

Очищает старые `apply_versions` rows и versioned dataplane manifests в `generated/dataplane/`.

## Важные функции

- `cleanup_apply_versions_retention(...)`
  Удаляет старые apply version rows, их manifest files и orphan manifests. Для versioned manifests retention требует одновременно age и count overflow: последние версии и свежая диагностика сохраняются.

## Внешние зависимости

- SQLite `apply_versions`
- `generated/dataplane`

## Runtime/persistent state

- уменьшает рост `/var/lib/fwrouter-v2/fwrouter.db`
- уменьшает рост versioned manifest churn в `generated/dataplane`

## Boot persistence relevance

Высокая.

## Нюансы

- manifest files должны оставаться синхронны с apply_versions rows
- current/applied/last-good artifacts не трогаются
- orphan manifests считаются только для файлов без строки в `apply_versions`, чтобы dry-run не завышал reclaimable bytes двойным учетом
