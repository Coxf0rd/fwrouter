# `/opt/fwrouter-api/fwrouter_api/services/live_probe_cache.py`

## Назначение

Общий in-process short-TTL cache для дорогих live probes и runtime aggregations.

## Важные функции

- `get_live_probe_cache(key, ttl_seconds, loader)`
  Возвращает cached value или вызывает `loader()`.
  TTL должен начинаться после завершения `loader()`, иначе длинные probes протухают еще до первого ответа.

- `clear_live_probe_cache()`
  Глобально очищает cache. Критично вызывается после `apply/reconcile`, чтобы runtime APIs не показывали stale state.

## Внешние зависимости

- `threading.Lock`
- `time.monotonic`

## Runtime/persistent state

- только in-memory process cache
- на диск ничего не пишет

## Boot persistence relevance

Низкая для boot itself, но высокая для эксплуатационной диагностики: от правильной cache semantics зависит latency `/runtime`, `/system/summary`, `/ui/*`.

## Нюансы

- этот cache не является source of truth
- использовать только для read-only probes и summary builders
- длинный TTL здесь опасен: можно скрыть недавний `apply` или runtime drift
- важный invariant: slow loader не должен “съедать” весь TTL до первой выдачи значения
- cache часто прогревается отдельно через best-effort prewarm, но prewarm не заменяет cold rebuild on demand
