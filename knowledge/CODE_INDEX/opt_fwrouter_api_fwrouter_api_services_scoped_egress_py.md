# `/opt/fwrouter-api/fwrouter_api/services/scoped_egress.py`

## Назначение

Строит runtime-представление scoped egress по subjects: eligibility, matcher resolution, applied/pending status, xray bindings и resolved VPN target projection.

## Важные функции

- `_load_xray_bindings()`
  Читает `/var/lib/fwrouter-v2/xray/fwrouter-bindings.json`.

- `_matcher_from_subject(subject)`
  Преобразует subject detail в matcher contract для IP/client identity.

- `_ip_matcher(candidate_ip, ...)`
  Строит matcher для IPv4/IPv6.

- `build_scoped_subject_runtime(...)`
  Основная функция статуса для одного subject. Принимает capture path и separate VPN target fields.

## Внешние зависимости

- settings paths
- xray bindings file
- subject detail schema
- subject_policy effective-state projection

## Runtime/persistent state

- читает persistent/generated xray bindings
- не меняет live kernel state

## Boot persistence relevance

Средняя/высокая. Влияет на post-boot восстановление selective subject semantics и diagnostics.

## Нюансы

- не все subject identity types materialize'ятся в v1
- часть subjects остается `pending_*` по design, а не из-за ошибки
- `vpn_target_id/source` и `selected_server_id/source` теперь разделены по смыслу; scoped egress использует resolved VPN target для runtime projection
- `lan` и `tailscale_node` с `dataplane_path=selective` теперь считаются materialized scoped runtime path через `nft_subject_classify`, даже если конкретный `selected_server_id` ещё не фиксируется как отдельный scoped VPN target
- diagnostics/readiness теперь отдельно маркируют случай `selective_materialized_but_transparent_tcp_unhealthy`: matcher/classify already materialized, но transparent TCP contour Mihomo ещё не здоров
- Xray semantics остаются отдельными: Xray subject считается materialized только через runtime bindings file, а не по `nft` classify
- Xray binding readiness не блокируется generic transparent `vpn_supported`: explicit Xray runtime/handoff проверяется по `fwrouter-bindings.json`, иначе Xray может ошибочно отображаться как `pending_missing_vpn_runtime`
