# `/opt/fwrouter-api/fwrouter_api/services/state_retention.py`

## Назначение

Очищает старые filesystem snapshots и backup/debug artifacts в `/var/lib/fwrouter-v2`.

## Важные функции

- `cleanup_state_retention(...)`
  Главная retention-точка для dataplane snapshots, transfer snapshots, DB backups и debug artifacts.

## Runtime/persistent state

- `last-good/dataplane/snapshots`
- `transfer/control-plane-snapshot.*.json`
- `backups/*.bak`
- `debug/*`

## Нюансы

- dataplane snapshots используют conservative policy: кандидат удаляется только если он старше retention window и не входит в последние N snapshots.
- snapshots с `plan_id` текущих `current/applied/last-good` manifests защищены от удаления.
- отчет содержит candidate/deleted bytes, чтобы maintenance dry-run показывал реальный storage effect.
