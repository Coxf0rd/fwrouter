# `/opt/fwrouter-api/fwrouter_api/services/apply.py`

## Назначение

Низкоуровневый apply pipeline для rendering manifests, preflight, dataplane adapter calls, rollback discipline и artifact writes.

## Важные классы

- `ApplyMode`
- `ApplyPhaseTimeoutError`
- `ApplyJobAbortedError`
- `ApplyPhaseTracker`

## Важные функции

- `build_apply_plan(...)`
- phase tracking helpers
- runtime/result manifest persistence
- orchestration around dataplane adapter operations
- `run_apply_pipeline(..., prebuilt_manifest=...)`
  Теперь умеет принимать уже подготовленный manifest template для fast activation global profiles и в apply-time только обновляет volatile поля `plan_id/reason/generated_at/input`.
- runtime verify не запускает read-model prewarm; heavy compilation of all global profiles планируется после successful apply только для mutations, которые могут инвалидировать profile stamp, и не запускается для `set_global_mode`.
- runtime verify после `apply` обязан сравнивать live contour с **requested** mode текущего apply, а не со stale `applied_mode` из routing snapshot
- subject-only fast apply на фоне `global=direct` использует `fwrouter_classify` hot-swap path для одиночных LAN/Tailscale `direct/selective/vpn` toggles: после candidate render/check заменяется только classify chain, а full table/sets/counters не пересоздаются. Runtime verify проверяет expected scoped marker конкретного subject вместо full global live probe.
- для `domain-aware selective` subject hot-swap сначала проверяет `dnsmasq selective` contract; `reconcile_dnsmasq_rules()` запускается только если contract unhealthy. При fallback на full `nft` apply старый reconcile path остается обязательным, потому что пересоздание `inet fwrouter_v2` может оставить live `dnsmasq nftset` references stale.
- после full `nft` apply live mode probe имеет короткий retry перед rollback. Это защищает от transient `unknown` сразу после замены `inet fwrouter_v2`: успешный apply не должен откатываться только из-за краткого окна чтения live chain.
- `set_global_mode` имеет hot-swap path для уже готовой live owned table: после `nft -c` pipeline может заменить только chain `fwrouter_classify` через atomic `nft -f` (`flush chain` + `add rule ...`) и не пересоздавать sets/counters. Это сохраняет `dnsmasq nftset` references и пропускает `dnsmasq reconcile`; если table/chains или VPN policy-routing contract не готовы, используется старый full apply. После hot-swap pipeline читает live chain и сверяет rule comment markers; при mismatch делает одну повторную попытку и не должен silently promote рассинхронизированный chain.

## Внешние зависимости

- dataplane adapter
- DB/jobs state
- manifest/artifact services
- dataplane live/status/global
- dnsmasq reconcile

## Runtime/persistent state

- пишет artifacts, results, manifests
- запускает live dataplane apply/rollback

## Boot persistence relevance

Критическая. Это ядро controlled materialization persistent intent в live host state.

## Нюансы

- для `global vpn` verify pipeline использует explicit mode override, чтобы не откатывать успешный `vpn` apply только потому, что в `routing_global_state` еще лежит старый `applied_mode`
- `dnsmasq` reconcile запускается после успешного nft apply для domain-aware selective/vpn contours
- `fast_subject_apply` не обходит full apply, если live owned table/chains или policy-routing contract не готовы; тогда pipeline падает обратно на обычный full table apply и `dnsmasq` lifecycle.
- prebuilt manifest не отменяет `check/apply/verify`; он сокращает только render/build phase, а не live safety gates
- full global profile compilation не должна стартовать до завершения критических `check/apply/verify/promote` фаз и не нужна для простого `set_global_mode`, иначе global mode switch снова упирается в CPU/IO contention с пересборкой profiles
- global mode hot-swap сознательно не используется для core bypass, missing table/chains или неготового VPN policy-routing contract; эти случаи требуют full apply/repair discipline. Hot-swap success нельзя считать валидным без live marker verification.
