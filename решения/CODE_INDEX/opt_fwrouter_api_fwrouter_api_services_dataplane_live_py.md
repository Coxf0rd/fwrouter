# `/opt/fwrouter-api/fwrouter_api/services/dataplane_live.py`

## Назначение

Минимальный live-probe слой, который напрямую читает `nft chain` и определяет текущий глобальный режим dataplane.

## Важные функции

- `probe_live_global_mode()`
  Кэшированный probe live global mode.

- `applied_nft_markers_match_live(...)`
  Сравнивает критичные comments из `applied.nft` со всей live table `inet fwrouter_v2`.
  Сейчас критичными считаются scoped subject rules и transparent/VPN handoff policy markers; отсутствие marker означает, что live kernel table drift'нул от applied artifact.

- `_probe_live_global_mode_uncached()`
  Анализирует `nft list chain inet fwrouter_v2 fwrouter_classify`.

- `live_mode_matches_intent(...)`
  Сравнивает live mode с expected intent.

## Внешние зависимости

- `nft`
- active Mihomo config для `resolved_selective_default`

## Runtime/persistent state

- read-only по отношению к системе

## Boot persistence relevance

Высокая. Именно этот слой обнаруживает drift между live table и persisted routing intent.

## Нюансы

- markers в `nft` comments входят в explicit live-consistency contract и не должны меняться без обновления probe logic
