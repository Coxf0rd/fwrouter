# `/opt/fwrouter-api/fwrouter_api/adapters/dataplane.py`

## Назначение

Adapter layer между Python apply pipeline и shell dataplane helpers.

## Важные функции

- `DataplaneOperation`
  Enum операций `check`, `apply`, `rollback`.
- `DataplanePlan`
  Metadata rendered dataplane plan: пути к generated candidate, manifest, rollback/snapshot artifacts.
- `DataplaneResult`
  Единый результат выполнения dataplane operation.
- `NftOwnedTableAdapter`
  Запускает `dataplane-check`, `dataplane-apply`, `dataplane-rollback` через script runner и сохраняет stdout/stderr как job artifacts.

## Внешние зависимости

- `adapters/scripts.py`
- `services/dataplane_nft.py`
- `services/dataplane_status.py`
- `services/artifacts.py`
- `/usr/local/libexec/fwrouter/dataplane-*.sh`

## Runtime/persistent state

- пишет job artifacts в `/var/lib/fwrouter-v2/jobs/<job_id>/`
- читает generated dataplane artifacts, переданные в `DataplanePlan`
- live kernel state меняют только shell helpers, не сам adapter

## Boot persistence relevance

Высокая. Этот слой является частью apply/recovery path для owned `nftables` table и policy routing.

## Нюансы

- stdout/stderr dataplane helper-ов нужно сохранять как artifacts: это основной материал для разбора failed apply.
- Adapter не должен подменять contract shell helpers; он только нормализует результат и error fields.

