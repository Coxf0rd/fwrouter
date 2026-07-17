# `/usr/local/libexec/fwrouter/dataplane-rollback.sh`

## Назначение

Откатывает owned table и policy routing к snapshot/last-good состоянию.

## Важные функции

- удаляет current `fwrouter_v2`
- восстанавливает snapshot, если он есть
- loop-delete legacy `ip rule` для `0x100`, `0x102` и `0x200`
- заново materialize'ит routing contract, если restored snapshot/manifest требует VPN policy routing; rollback больше не должен терять `table 100` только потому, что global mode был `direct`

## Внешние зависимости

- `nft`
- `ip`
- snapshot paths

## Runtime/persistent state

- меняет live kernel dataplane

## Boot persistence relevance

Высокая, потому что rollback discipline определяет надежность apply pipeline.
