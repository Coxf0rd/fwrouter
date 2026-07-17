# `/opt/fwrouter-api/fwrouter_api/services/control_plane_transfer.py`

## Назначение

Экспорт, валидация, планирование и импорт control-plane snapshot для переноса состояния.

## Важные функции

- `_export_subjects()`
  Экспортирует subjects с detail-таблицами.

- `_redact_subscription_state(...)`
- `_redact_custom_https_proxy_rows(...)`
  Убирают секреты при экспорте, если snapshot не должен содержать credentials.

- `_export_rules_bundle()`
  Экспортирует state, metadata и текстовые/JSON артефакты правил.

- `_resolve_transfer_snapshot_path(file_path)`
  Жестко ограничивает snapshot paths директорией `state_dir/transfer`.

- `resolve_control_plane_snapshot_source(...)`
  Общая точка для payload/file-based import source.

## Внешние зависимости

- DB
- runtime summary/scoped egress summary
- rules/services/system summary
- filesystem `transfer` directory

## Runtime/persistent state

- пишет snapshot files в `/var/lib/fwrouter-v2/transfer`
- читает существенную часть persistent state

## Boot persistence relevance

Средняя. Полезен для migration/backup/restore, но не участвует в штатном boot path.

## Нюансы

- path confinement внутри transfer dir нельзя ослаблять
- redaction behavior важен для безопасного экспорта
