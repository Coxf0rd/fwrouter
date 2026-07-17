# `/opt/fwrouter-api/fwrouter_api/services/dataplane_status.py`

## Назначение

Строит runtime enforcement state на основе live dataplane payload, bypass state, applied manifest и preflight contract. Также читает live transparent-path counters (`vpn mark -> redirect/tproxy handoff`) из `nft`.

## Важные функции

- `_runtime_check_paths()`
  Выбирает candidate/applied paths для runtime probe.
  Для applied runtime probe возвращает пустой candidate path и applied manifest: read-only status не должен делать `nft -c -f last-good.nft`, потому что это дорогая validation уже применённого большого файла.

- `read_live_dataplane_payload()`
  Кэшированный вызов `dataplane_check`, теперь дополненный `transparent_path` diagnostics и `artifact_consistency`.
  Если live table не содержит критичные marker-comments из `applied.nft`, payload помечается `ok=false` с `LIVE_DATAPLANE_ARTIFACT_DRIFT`.

- `inspect_transparent_path_counters()`
  Читает live counters по comment-contract:
  - `fwrouter vpn mark tcp:5202`
  - `fwrouter vpn mark udp:5203`
  - `fwrouter redirect handoff tcp:5202`
  - `fwrouter tproxy handoff udp:5203`
  - `fwrouter vpn mark tcp:5204`
  - `fwrouter vpn mark udp:5205`
  - `fwrouter full-vpn redirect handoff tcp:5204`
  - `fwrouter full-vpn tproxy handoff udp:5205`
  и строит verdict, дошел ли трафик до transparent handoff.
  Реализация читает каждый уникальный nft chain один раз и затем матчится по comment-prefix, чтобы не плодить `nft list chain` subprocess на каждый отдельный counter pattern.

- `build_runtime_enforcement_state(...)`
  Главный runtime aggregator для dataplane capability/enforcement level.
  Если `live_payload` передан явно, считает без cache. Если нет, использует short-TTL cache, потому что внутри есть expensive preflight/live probes.
  Также умеет принимать уже добытый `mihomo_health`, чтобы caller не выполнял второй identical Mihomo probe только ради preflight.

- `get_dataplane_capability()`
  Convenience accessor для capability-only callers.

## Внешние зависимости

- script runner `dataplane_check`
- core bypass state
- applied manifest
- dataplane live probe
- routing global state

## Runtime/persistent state

- только читает artifacts и live kernel status

## Boot persistence relevance

Критическая как unified runtime truth для API, bootstrap и diagnostics.

## Нюансы

- при наличии applied manifest state пересчитывается по live runtime, а не слепо доверяет historical snapshot
- owned-table-ready и applied-routing-intent это разные вещи
- cache здесь безопасен только потому, что `apply` path после мутаций очищает `live_probe_cache`
- runtime/status path intentionally не валидирует last-good nft candidate; строгая candidate validation остаётся в apply/check pipeline
- для `/runtime` критично не дублировать Mihomo probe: иначе cold latency растет почти линейно на второй такой же health call
- сам факт listener `:5202`/`:5204` больше не считается достаточным доказательством здоровья transparent path; runtime теперь обязан различать `vpn_mark_packets`, TCP redirect handoff и UDP tproxy handoff для selective и full-VPN contours
