# `/opt/fwrouter-api/fwrouter_api/services/runtime.py`

## Назначение

Собирает read-only runtime summary по backend, modules, routing, dataplane, Mihomo, Xray, tailscale, subjects и traffic.

## Важные функции

- `_cached_mihomo_health()`
- `_cached_xray_health()`
  Кэшируют probe results на очень короткое время, чтобы runtime endpoint не перегружал адаптеры.
  `Mihomo` probe intentionally разделяет cache key с `dataplane_global`, чтобы `/runtime` не делал второй такой же health probe внутри `build_global_preflight()`.

- `_build_runtime_summary()`
  Тяжелый uncached агрегатор.
  Самые дорогие cold probes (`mihomo`, `xray`, `tailscale`, `subscription`, `modules`, `dataplane_check`) теперь запускаются параллельно через `ThreadPoolExecutor`, чтобы сокращать cold latency без отказа от live diagnostics.

- `get_runtime_summary()`
  Обертка над `_build_runtime_summary()` с short-TTL cache через `live_probe_cache`.
  Это важно для UI polling: первый запрос может занять ~2s, повторный в пределах TTL должен быть быстрым.

- `_project_module_runtime(...)`
  Проецирует runtime state поверх DB state для `core`, `vpn`, `xray`.

## Внешние зависимости

- Mihomo/Xray adapters
- DB schema inspector
- dataplane status/live payload
- tailscale probe
- traffic accounting
- scoped egress diagnostics

## Runtime/persistent state

- читает generated manifests и last-good nft path
- не должен менять live dataplane

## Boot persistence relevance

Высокая как основная точка диагностики boot drift и readiness.

## Нюансы

- этот файл не просто status endpoint; по нему удобно проверять drift между intended и live state
- `automation` section теперь разделяет:
  - `startup_foundation`
  - `startup_live_recovery`
  - `startup_apply_reconcile`
  - `startup_dnsmasq_reconcile`
  - `runtime_convergence_scheduler`
  - `maintenance_scheduler`
  - `watchdog_scheduler`
  Старый `startup_recovery` остается как compatibility alias
- projected module states не всегда совпадают с тем, что лежит в таблице `modules`
- если runtime summary снова стал медленным, сначала проверять `build_runtime_enforcement_state()` и cache semantics в `live_probe_cache.py`, а не Mihomo controller
- если cold `/runtime` снова распух, сначала проверять:
  - не сломался ли shared Mihomo cache key между `runtime.py` и `dataplane_global.py`
  - не перестал ли работать параллельный probe fan-out в `_build_runtime_summary()`
- первый запрос после очистки cache все еще зависит от live probes; отдельный prewarm только уменьшает шанс, что пользователь сам заплатит этот cost
