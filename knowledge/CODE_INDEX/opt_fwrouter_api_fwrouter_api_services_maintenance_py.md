# `/opt/fwrouter-api/fwrouter_api/services/maintenance.py`

## Назначение

Сводит штатный control-plane maintenance: retention для jobs, logs, state, traffic, apply_versions и job-result compaction.

## Важные функции

- `run_control_plane_maintenance(...)`
  Главный orchestration path для periodic cleanup. Возвращает filesystem storage snapshot до/после и reclaimable estimate для крупных artifact buckets.
- `_maintain_database_storage(...)`
  При необходимости делает `VACUUM`/`PRAGMA optimize` после массовых deletions или compaction.
- `cleanup_xray_legacy_subscription_shadows(...)`
  Находит старые inactive Xray subjects с email `<token>@fwrouter.local`, если такой token уже существует в `subscription_clients`, и при реальном maintenance помечает их `is_deleted=1`.
  Правило намеренно узкое: не трогает active rows, rows с `last_traffic_at`, `sub-*` subscription runtime profiles и `vpn-auto-*` service subjects.

## Внешние зависимости

- `jobs_retention`
- `logs_retention`
- `state_retention`
- `traffic` cleanup
- `apply_versions_retention`
- job-result compaction
- `subscription_clients` для точечной очистки shadow-дублей Xray subscription profiles

## Runtime/persistent state

- читает и чистит SQLite jobs/apply_versions
- уменьшает write-churn и filesystem growth
- считает размеры `jobs`, `generated/dataplane`, `last-good/dataplane/snapshots`, чтобы dry-run был пригоден для knowledge о cleanup

## Boot persistence relevance

Высокая.

## Нюансы

- maintenance теперь не только симулирует cleanup, но и реально уменьшает sqlite/file footprint
- `xray_legacy_shadows` в отчете maintenance показывает кандидатов и число soft-delete; destructive path запускается только при `dry_run=false`
- vacuum запускается после реальных deletions/compaction, чтобы DB файл мог схлопнуться
- byte accounting в отчете не является source of truth для routing; это diagnostic surface для безопасной очистки
