# `/opt/fwrouter-api/fwrouter_api/services/routing_manifest.py`

## Назначение

Собирает bounded manifest для apply pipeline: global routing intent, effective subject state, preflight summary и явные dataplane contracts, которые потом обязаны одинаково читать renderer, shell apply/check и runtime diagnostics.

## Важные функции

- `build_dataplane_manifest_from_state(...)`
  Главная точка сборки manifest из routing state, subject inventory и bounded extra data. Перед записью subject entries теперь заново нормализует их через `enrich_subject_with_effective_state(...)` с planned `runtime_enforcement`, чтобы manifest не смешивал stale live subject-binding (`dataplane_path=direct`) с новым healthy `global_preflight` (`selective=true`).

- `_requires_vpn_policy_routing(...)`
  Вычисляет, нужен ли transparent VPN policy-routing contract реально, а не только по `global_mode`. Это критично для `global=direct + scoped selective/vpn`.

## Нюансы

- manifest несет `summary.requires_vpn_policy_routing` как canonical source of truth для shell apply/check и runtime diagnostics
- `write_dataplane_manifest(...)` пишет canonical candidate manifest один раз, versioned manifest делает atomic copy, а root-level job manifest теперь оставляет компактным summary/link. Это важно для больших precompiled global profiles: не нужно плодить лишние 20-30MB copies на каждый apply.
- bounded `scoped_runtime` в manifest сохраняет legacy `state=disabled` alias для not-applicable subjects и отдельно несет `status`, чтобы старые consumers не ломались при новом scoped-runtime contract
- subject entries внутри manifest не должны доверять уже вложенному `effective_state`, если он был посчитан на другом runtime snapshot; source of truth для render/apply это единый planned `global_preflight` / `runtime_enforcement`
- Xray `forced_vpn` subjects не должны сами по себе поднимать transparent nft/policy-routing contract: они идут через explicit Xray runtime contour, а не через LAN `redirect/tproxy` ingress
- transparent listener bind не считается отдельным manifest decision surface; renderer и Mihomo contract используют documented wildcard `0.0.0.0`
