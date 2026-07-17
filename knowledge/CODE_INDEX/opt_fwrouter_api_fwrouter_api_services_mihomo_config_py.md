# `/opt/fwrouter-api/fwrouter_api/services/mihomo_config.py`

## Назначение

Генерирует, валидирует, продвигает и reconcile'ит `mihomo` config, включая transparent contour и xray handoff listeners.

## Важные функции

- `_resolved_candidate_config_path()`
- `_resolved_base_config_path()`
- `_resolved_applied_manifest_path()`
  Нормализуют пути generated artifacts.

- `_resolve_proxy_bypass_mark_value()`
  Достает bypass mark из manifest, fallback `512`.

- `_managed_transparent_redir_port()` / `_managed_transparent_tproxy_port()`
  Берут canonical transparent ingress ports из contours/runtime metadata. Теперь канонический split такой: `fwrouter-redir:5202` для TCP и `fwrouter-tproxy:5203` для UDP.

- `_managed_full_vpn_redir_port()` / `_managed_full_vpn_tproxy_port()`
  Возвращают always-on full-VPN ports `5204/5205` для scoped/global full VPN path.

- `_build_managed_transparent_listeners(bind_address)`
  Строит named transparent listeners с dispatch в `rule: fwrouter-transparent`:
  - `fwrouter-redir` (`type: redir`, `port: 5202`)
  - `fwrouter-tproxy` (`type: tproxy`, `port: 5203`, `udp: true`)
  и full-VPN listeners с direct dispatch в `vpn-global`:
  - `fwrouter-full-redir` (`type: redir`, `port: 5204`)
  - `fwrouter-full-tproxy` (`type: tproxy`, `port: 5205`, `udp: true`)

- `_resolve_transparent_bind_address()`
  Предпочитает явный router IPv4 из `dnsmasq` selective preflight и only then откатывается к wildcard `0.0.0.0`.

- `validate_mihomo_candidate_config(...)`
  Структурно валидирует generated candidate. Canonical transparent contract теперь состоит из пары named listeners `fwrouter-redir/fwrouter-tproxy` с `rule: fwrouter-transparent` или legacy-compatible `proxy: vpn-global` и valid bind-address; loopback bind по-прежнему отклоняется через `MIHOMO_TRANSPARENT_LISTENER_BIND_INVALID`.
- `get_mihomo_config_status(include_config=False)`
  По умолчанию возвращает быстрый summary (`exists`, `mtime`, `rules_count`) без полного `yaml.safe_load()` огромных `config.yaml/config.next.yaml`. Для ручной глубокой диагностики можно передать `include_config=True`, но это дорогой путь на больших rulesets.
- `mihomo_runtime_satisfies_routing(routing)`
  Cheap verifier для apply-orchestrator: читает bounded `fwrouter` metadata из активного `config.yaml`, проверяет Mihomo health/selectors и split transparent contour. Нужен, чтобы обычная смена `global_mode` не генерировала и не валидировала заново 100k+ rule YAML, если runtime уже соответствует routing-owned contract.
- `_ensure_fwrouter_sniffer(base_config)`
  Принудительно нормализует sniffer для transparent TCP: `force-dns-mapping`, `parse-pure-ip`, global `override-destination` и per-protocol `HTTP/TLS/QUIC override-destination`. Это страховка на случай, когда `redir` contour не восстанавливает original destination и Mihomo должен доопределить целевой домен по Host/SNI вместо `127.0.0.1:5202`.

- `reconcile_mihomo_runtime(...)`
  Ключевой apply/reconcile entrypoint. При fallback-сравнении active/candidate config пытается получить full config через `include_config=True`, но сохраняет совместимость со старыми/test status providers без этого аргумента.

## Внешние зависимости

- YAML
- generated manifests
- last-good snapshots
- runtime restart Mihomo container
- xray handoff assignments

## Runtime/persistent state

- пишет `config.next.yaml`, `config.yaml`, `contours.json`
- использует last-good и debug dirs

## Boot persistence relevance

Высокая. Без корректного generated config Mihomo не поднимется после reboot.

## Нюансы

- `vpn-global` и `vpn-auto` являются частью contract
- runtime `proxies` должны строиться по union: active `global_list=1` ИЛИ active `vpn_auto=1`; auto-only сервер не должен пропадать из Mihomo только потому, что скрыт из ручного глобального списка
- custom proxy (`server_custom_https_proxy`, например `Proxy6`) должен оставаться в `vpn-auto`/`vpn-global` selector targets, если включены соответствующие preferences. Исключать его из групп нельзя: `vpn_auto_priority=-1` уже означает "ручной target, но не auto-selectable".
- `routing-mark` в config должен соответствовать bypass mark
- FWRouter explicit listener `fwrouter-mixed` должен существовать ровно один раз на `127.0.0.1:5201` и вести в `vpn-global`
- canonical transparent contour теперь split-listener:
  - `fwrouter-redir` (`type: redir`, `port: 5202`, `rule: fwrouter-transparent`)
  - `fwrouter-tproxy` (`type: tproxy`, `port: 5203`, `rule: fwrouter-transparent`, `udp: true`)
  - `fwrouter-full-redir` (`type: redir`, `port: 5204`, `proxy: vpn-global`)
  - `fwrouter-full-tproxy` (`type: tproxy`, `port: 5205`, `proxy: vpn-global`, `udp: true`)
  - `listen` должен быть `0.0.0.0` или explicit non-loopback IPv4 router bind
- `sub-rules["fwrouter-transparent"]` повторно применяет domain-aware правила после sniffing. Fallback должен быть `MATCH,DIRECT` при `selective_default=direct` и `MATCH,vpn-global` при `selective_default=vpn` или `vpn` mode.
- scoped LAN/Tailscale `vpn` не меняет global fallback и больше не добавляет source-CIDR override в `fwrouter-transparent`; nft выбирает `fwrouter_vpn_full`, который ведёт в always-on `fwrouter-full-redir/fwrouter-full-tproxy`
- canonical transparent dispatch теперь идёт через listener `rule: fwrouter-transparent`, а не через top-level `IN-PORT` rules и не через unconditional listener `proxy: vpn-global`
- legacy top-level inbound keys `mixed-port/port/socks-port/redir-port/tproxy-port` не должны resurrect-иться из `config.yaml`, `last-good` или debug snapshots
- managed listeners `fwrouter-redir/fwrouter-tproxy/fwrouter-full-redir/fwrouter-full-tproxy` должны регенерироваться builder'ом, а старые конфликтующие варианты из base config должны вычищаться
- transparent bind не должен быть loopback-bound: `127.0.0.1` ломает transparent path, даже если explicit proxy contour жив; валидны wildcard `0.0.0.0` и explicit non-loopback IPv4 router bind
- generated config должен принудительно держать `ipv6: false`; иначе `mihomo` может фактически открыть transparent ingress как IPv6-only listener (`[::]:5202`) и IPv4 LAN transparent ingress перестанет материализоваться в Mihomo connections
- generated config должен принудительно держать FWRouter-managed sniffer flags для pure-IP transparent traffic: `force-dns-mapping`, `parse-pure-ip`, global/per-protocol `override-destination`. Без этого live `redir` TCP может остаться привязанным к локальному destination (`127.0.0.1:5202`) и домен-aware selective routing не материализуется для app traffic вроде Instagram.
- mixed/controller listeners могут оставаться loopback-bound; это не тот же самый контракт, что у transparent ingress
