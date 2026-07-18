# `/opt/fwrouter-api/fwrouter_api/services/subject_policy.py`

## Назначение

Строит effective policy для subjects: capture mode, selective_default, VPN target binding, user overrides, global inheritance и scoped runtime projection.

## Важные функции

- `get_routing_snapshot()`
  Возвращает committed routing snapshot для effective-state builders.

- `resolve_selective_default(...)`
  Возвращает fallback для selective capture decision; не выбирает VPN target.

- `resolve_effective_capture_mode(...)`
  Разделяет persisted/admin/user mode и source для capture decision.

- `resolve_effective_vpn_target(...)`
  Выбирает VPN target отдельно от capture decision.

- `_load_active_user_override(...)`
- `_load_active_server_override(...)`
  Читают временные subject overrides.
  Bulk list path должен передавать `None` как known-absent override, а не как “аргумент не передан”; для этого используется sentinel. Иначе `resolve_effective_capture_mode()` снова делает per-subject DB lookup и `/ui/clients` получает N+1 latency.

- `_effective_mode(...)` и `_effective_mode_with_override(...)`
  Совместимые обертки для capture mode.

- `_effective_binding(...)`
  Превращает capture mode + VPN target binding в dataplane path и runtime projection.
- `enrich_subject_with_effective_state(...)`
  Должен получать согласованный `runtime_enforcement`, если вызывается из render/apply path. Иначе можно получить drift: subject хранит `effective_mode=selective`, но из stale live-enforcement материализуется как `dataplane_path=direct`.
- `_default_subject_runtime_enforcement(...)`
  Read-model default для `get/list_subjects_with_effective_state()`: строит planned enforcement из `build_global_preflight(...)`, а не из active global mode snapshot. Это нужно, чтобы UI/API не показывали `direct` для scoped selective subject только потому, что global mode сейчас `direct`.

## Внешние зависимости

- DB
- runtime enforcement state
- core bypass state
- scoped egress runtime builder
- routing global state

## Runtime/persistent state

- в основном читает DB и строит computed state

## Boot persistence relevance

Высокая. Именно этот слой определяет, что должно пережить reboot для каждого subject.

## Нюансы

- `fwrouter` subjects всегда `direct` по architectural invariant
- `selective_default` это capture fallback, а не VPN target
- per-client `selective` для transparent ingress subjects из `subject_taxonomy.TRANSPARENT_INGRESS_CLIENT_SUBJECT_TYPES` нельзя демотить в `direct` только потому, что global/domain selective contract временно degraded. Готовность runtime должна отражаться в `runtime_enforcement` и `scoped_runtime.status`, но `dataplane_path` должен оставаться `selective`, чтобы manifest смог materialize subject-aware nft classifier.
- `xray` semantics отличаются от `lan/tailscale`
- user mode override разрешён только для transparent ingress subjects; `xray` user override запрещён и не должен проходить через dataplane/apply path
- путать admin mode, user override и VPN target нельзя
- `list_subjects_with_effective_state()` не должен делать N+1 через `get_subject()` или per-subject override lookups поверх уже загруженных bulk maps; это влияет на cold latency `ui/clients`
