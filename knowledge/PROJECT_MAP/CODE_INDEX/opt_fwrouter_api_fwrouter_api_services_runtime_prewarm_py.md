# `/opt/fwrouter-api/fwrouter_api/services/runtime_prewarm.py`

## Назначение

Best-effort асинхронный прогрев read-only runtime/UI caches после startup и после cache invalidation.

## Важные функции

- `warm_runtime_read_models(include_global_profiles=True)`
  Прогревает `runtime`, `system_summary`, `ui_clients`, `ui_router_summary`, `ui_settings_workspace`. При `include_global_profiles=True` также пересобирает precompiled global profiles.

- `prime_runtime_read_models_async(include_global_profiles=True)`
  Запускает один daemon thread prewarm и не дает плодить параллельные прогревы.

## Внешние зависимости

- `runtime.py`
- `system_summary.py`
- `ui_state.py`

## Runtime/persistent state

- только in-memory cache warmup
- пишет rebuildable generated profiles в `generated/dataplane/profiles/`
- persistent intent не меняет

## Boot persistence relevance

Низкая для recovery, средняя для UX: уменьшает вероятность, что первый пользовательский запрос после startup/apply будет cold.

## Нюансы

- это optimization-only слой
- ошибки внутри prewarm intentionally suppress'ятся
- prewarm не гарантирует мгновенность, если пользователь пришел раньше завершения warmup
- precompiled profiles создаются здесь же, чтобы user-facing global mode switch чаще попадал в fast activation path
- critical apply/UI paths должны использовать `include_global_profiles=False`; пересборка всех profiles тяжёлая и не должна конкурировать с интерактивной сменой режима
- startup также использует `include_global_profiles=False`; profile rebuild должен происходить post-apply только для изменений, которые реально инвалидируют global profile stamp, а не на каждом restart API
