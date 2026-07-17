# `/opt/fwrouter-api/fwrouter_api/services/dataplane_global.py`

## Назначение

Описывает канонический host dataplane contract: protected networks, service domains, fwmarks, table id, Mihomo contour profile.

## Важные функции

- `build_vpn_steering_contract(redir_port=..., tproxy_port=...)`
  Возвращает contract с base mark `0x100`, bypass mark `0x200`, table `100`, priority `100`, selector `vpn-global` и split transparent ingress metadata. Full-VPN UDP mark `0x102` derived из base mark в nft/shell слоях.

- `build_dataplane_profile(...)`
  Собирает полный профиль ownership: table name, Mihomo ports, selective path kind, vpn contract и раздельные `transparent_tcp_ready` / `transparent_udp_ready`.

- `read_effective_rules_artifact()`
- `read_applied_manifest()`
  Читают artifacts из persistent state через short-TTL cache. `read_effective_rules_artifact()` сначала использует `generated/rules/effective-rules.json`, затем fallback на `last-good/rules/effective-rules.json`; global/selective preflight не должен ложно падать после успешного apply, если generated rules artifact уже не лежит в hot generated dir, но last-good rules snapshot является актуальным source для live dataplane.

- `build_applied_runtime_enforcement(...)`
  Собирает финальный enforcement verdict. В apply-ветке может получать explicit `mode_override`, чтобы live verify опирался на целевой mode текущего transaction, а не на stale `applied_mode`.

- `build_global_preflight(...)`
  Строит runtime/preflight verdict для `direct/selective/vpn`. После фикса использует runtime признаки transparent contour и добавляет `mihomo_transparent_contour_not_ready` в VPN-path missing requirements, если live Mihomo contour structurally broken. Раздельно учитывает `transparent_tcp_listener_present/ready`, `transparent_udp_listener_present/ready`, counter-based `transparent_tcp_flow_observed/transparent_udp_flow_observed` и Mihomo session observation; больше не synthesise healthy contour только из `runtime_state=RUNNING` или legacy ports.

## Внешние зависимости

- Mihomo adapter health
- generated artifacts
- DB for custom proxy protected networks

## Runtime/persistent state

- сам файл не применяет kernel state, но определяет его контракт

## Boot persistence relevance

Критическая. Любое расхождение с shell scripts и generated configs ломает recovery.

## Нюансы

- protected/private ranges нельзя менять фрагментарно
- `src_valid_mark=1` и fwmark values логически принадлежат этому контракту
- `selective` по-прежнему может деградировать в `direct` fallback, но invalid transparent contour больше не считается здоровым `vpn` runtime
- canonical transparent contour теперь split:
  - TCP: `redir 5202`
  - UDP: `tproxy 5203`
  - full-VPN TCP: `redir 5204`
  - full-VPN UDP: `tproxy 5205`
  - preflight/profile обязаны не терять один из этих портов при degraded controller/runtime
  - для LAN/Tailscale selective success критичен именно healthy TCP contour, а не только UDP listener
- Android connectivity-check domains встроенно считаются forced-direct operational override. Это intentional exception к широким VPN feeds: интернет-check клиента не должен уходить в VPN только потому, что большой aggregate уже попал в `vpn_ipv4`
- `build_global_preflight()` это один из главных cold-path cost centers для `/api/v2/runtime`; любые новые проверки здесь нужно оценивать по latency
