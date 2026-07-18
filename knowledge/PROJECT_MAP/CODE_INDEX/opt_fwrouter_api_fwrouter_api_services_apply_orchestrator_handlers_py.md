# `/opt/fwrouter-api/fwrouter_api/services/apply_orchestrator_handlers.py`

## Назначение

Держит тяжелые mutation handlers для apply orchestration. После разрезания `apply_orchestrator.py` этот файл содержит runtime paths для global/subject/server override/manual rules mutations, а основной модуль остался фасадом и job entrypoint layer.

## Важные функции

- `_execute_set_global_mode(...)`
- `_execute_set_global_server_mode(...)`
- `_execute_set_selective_default(...)`
- `_execute_set_subject_admin_mode(...)`
- `_execute_set_subject_user_mode(...)`
- `_execute_set_subject_server_override(...)`
- `_execute_clear_subject_server_override(...)`
- `_execute_apply_manual_rules(...)`
- `_execute_repair_global_direct_runtime(...)`
- `execute_apply_mutation(...)`

Все эти функции используют helpers из `apply_orchestrator.py` через модуль-фасад, чтобы не ломать старые imports и monkeypatch paths в тестах.

## Внешние зависимости

- `apply_orchestrator.py` как facade/shared helper layer
- apply pipeline
- subject/server DB state
- Mihomo/Xray reconcile paths

## Boot persistence relevance

Высокая. Это фактический runtime path для immediate apply/repair mutations.

## Нюансы

- файл intentionally не владеет shared constants/DB helper logic; они оставлены в `apply_orchestrator.py`
- late/facade split сделан ради уменьшения размера файла без behavioral rewrite
- `set_subject_admin_mode` / `set_subject_user_mode` для LAN/Tailscale direct/selective/vpn теперь прокидывают `fast_subject_apply` hint в low-level apply pipeline; это позволяет subject-only toggle на фоне `global=direct` пройти через lighter verify path вместо полного global-probe цикла, но не отменяет обязательный `dnsmasq` runtime refresh для `domain-aware selective`
- `set_subject_admin_mode` принимает batch `subject_ids` для UI aggregate rows, сейчас используется для `xray-subscription:*`: handler валидирует, stage-ит и commit-ит режим всем реальным Xray subjects группы в одном apply job. `fast_subject_apply` для batch выключен.
- `set_subject_admin_mode` для LAN/Tailscale `direct/selective/vpn` больше не reconcile'ит Mihomo только ради смены режима. Selective listener-ы `5202/5203` и full-VPN listener-ы `5204/5205` должны быть warm/always-on; subject toggle меняет nft/dataplane steering.
- `set_selective_default` имеет fast-path для clean `global=direct`: если live drift отсутствует, handler только коммитит `routing_global_state.selective_default` и возвращает `runtime_state_unchanged=true`, без `reconcile_mihomo_runtime()` и без nft apply pipeline.
- applied manifest drift только по полю `selective_default` игнорируется в этом fast-path, потому что `global direct` enforcement не зависит от selective fallback. Другие drift mismatch-и по-прежнему требуют полного repair/apply path.
- `set_global_mode` для `selective/vpn` сначала вызывает cheap verifier `mihomo_runtime_satisfies_routing(...)`. Если активный Mihomo runtime уже `running`, selectors совпадают с routing и transparent contour ready, handler пропускает дорогой full `reconcile_mihomo_runtime()` с генерацией/валидацией огромного YAML и сразу идет в dataplane apply. При любом mismatch сохраняется старый полный reconcile path.
