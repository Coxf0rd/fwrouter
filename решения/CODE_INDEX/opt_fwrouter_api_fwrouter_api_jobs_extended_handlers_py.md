# `/opt/fwrouter-api/fwrouter_api/jobs/extended_handlers.py`

## Назначение

Регистрирует extended handlers второго слоя: maintenance, inventory, traffic, overrides expiry, rules full update.

## Важные функции

- `_compact_traffic_result(traffic)`
  Урезает persisted result для scheduled traffic collect jobs: counts/deltas/state без полного `processed` и без полного script stdout.
- `maintenance_cleanup_handler(job)`
- `expire_subject_overrides_handler(job)`
- `traffic_accounting_collect_handler(job)`
- `subject_inventory_sync_handler(job)`
- `rules_full_update_handler(job)`
- `register_extended_handlers(manager)`

## Внешние зависимости

- maintenance service
- subject policy expiry
- traffic collection
- subject inventory sync
- rules full update

## Runtime/persistent state

- зависит от конкретного handler; многие из них меняют DB/artifacts

## Boot persistence relevance

Средняя. Эти handlers питают post-boot maintenance и scheduled operations.
