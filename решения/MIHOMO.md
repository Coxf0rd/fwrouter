# Mihomo

## Роль в системе

Mihomo это основной transparent egress runtime. Через него идет global/selective/vpn steering.

## Основные файлы

- `/opt/fwrouter-mihomo/docker-compose.yml`
- `/var/lib/fwrouter-v2/generated/mihomo/config.yaml`
- `/var/lib/fwrouter-v2/generated/mihomo/config.next.yaml`
- `/var/lib/fwrouter-v2/generated/mihomo/contours.json`
- `fwrouter_api/services/mihomo_config.py`
- `fwrouter_api/adapters/mihomo.py`
- `fwrouter_api/services/mihomo_runtime.py`

## Runtime contract

- controller: `127.0.0.1:5200`
- mixed listener: `5201`
- transparent ingress теперь split и слушается через named listeners:
  - `fwrouter-redir` на `192.168.0.1|0.0.0.0:5202` для TCP
  - `fwrouter-tproxy` на `192.168.0.1|0.0.0.0:5203` для UDP
  - оба listener-а должны вести в `rule: fwrouter-transparent`, а не напрямую в `vpn-global`
  - `fwrouter-full-redir` на `192.168.0.1|0.0.0.0:5204` для full-VPN TCP
  - `fwrouter-full-tproxy` на `192.168.0.1|0.0.0.0:5205` для full-VPN UDP
  - full-VPN listener-ы ведут напрямую в `proxy: vpn-global` и не используют `rule: fwrouter-transparent`
  - `sub-rules["fwrouter-transparent"]` повторно применяет domain-aware правила после sniffing; fallback `MATCH,DIRECT` при `selective_default=direct`, `MATCH,vpn-global` при `selective_default=vpn` или `vpn` mode
  - scoped LAN/Tailscale `vpn` subjects больше не добавляются в Mihomo sub-rule как source-CIDR rules; full-VPN client mode выбирается в nft через `fwrouter_vpn_full -> 5204/5205`
  - runtime diagnostics обязаны различать отдельно:
    - `transparent_tcp_listener_present` / `transparent_tcp_ready`
    - `transparent_udp_listener_present` / `transparent_udp_ready`
    - `transparent_tcp_session_materialized` / `transparent_udp_session_materialized`
- selector group: `vpn-global`
- fallback selector target: `vpn-auto`
- runtime `proxies` должны включать все active servers, где `global_list=1` или `vpn_auto=1`
- `vpn-auto` и ручной `global-list` это разные списки:
  - `vpn-auto` selector содержит только auto-кандидатов плюс `DIRECT`
  - `vpn-global` selector содержит `vpn-auto`, ручные `global_list` targets и `DIRECT`
  - `vpn_auto_priority < 0` исключает сервер из автоматического выбора `Mihomo/watchdog`, даже если запись остается в более широком `vpn_auto` inventory для Xray/диагностики

## Boot relevance

- `fwrouter-mihomo.service` должен стартовать до API
- generated config должен существовать в persistent state
- backend startup восстанавливает selector state после restart/reboot

## Права

- `network_mode: host`
- `cap_add: NET_ADMIN, NET_RAW`
- device `/dev/net/tun`
- read-only mount generated config и rules dir

## Что нельзя ломать

- согласование `routing-mark` с bypass mark `512`
- наличие `vpn-global`
- canonical transparent contour теперь split-listener:
  - `fwrouter-redir` (`type: redir`, `port: 5202`, `rule: fwrouter-transparent`)
  - `fwrouter-tproxy` (`type: tproxy`, `port: 5203`, `rule: fwrouter-transparent`, `udp: true`)
  - `fwrouter-full-redir` (`type: redir`, `port: 5204`, `proxy: vpn-global`)
  - `fwrouter-full-tproxy` (`type: tproxy`, `port: 5205`, `proxy: vpn-global`, `udp: true`)
  - `listen` должен быть `0.0.0.0` или явный router IPv4, но не loopback
- explicit listener `fwrouter-mixed` остаётся отдельным `127.0.0.1:5201 -> vpn-global`
- subject mode apply для LAN/Tailscale `direct/selective/vpn` не должен reconcile'ить Mihomo config только ради смены режима: selective и full-VPN contours должны быть warm/always-on, а клиентский toggle реализуется через nft/dataplane
- если preflight знает router DNS IPv4, generated config может предпочесть bind именно к этому адресу вместо wildcard; это согласуется с `nft redirect to :5202` / `nft tproxy to :5203` и делает transparent endpoint менее двусмысленным
- generated FWRouter config должен явно держать `ipv6: false`, иначе `mihomo` может фактически открыть transparent ingress как IPv6-only socket и transparent IPv4 ingress для LAN-клиентов перестанет материализоваться в live sessions
- generated FWRouter config должен принудительно включать sniffer-профиль для transparent TCP recovery:
  - `sniffer.enable: true`
  - `sniffer.force-dns-mapping: true`
  - `sniffer.parse-pure-ip: true`
  - `sniffer.override-destination: true`
  - `HTTP/TLS/QUIC override-destination: true`
  Это нужно, когда `redir` ingress в live runtime теряет original destination и transparent TCP иначе материализуется как `127.0.0.1:5202` вместо sniffed SNI/Host.
- top-level `redir-port`/`tproxy-port` и main `IN-PORT` rules больше не являются каноническим способом pin'а transparent traffic; их роль заменена named listener-ами с `rule: fwrouter-transparent`
- health/preflight больше не должны silently synthesise healthy contour только из `runtime_state=RUNNING` или из старых top-level `tproxy-port`/`redir-port`; source of truth это parsed managed listeners `fwrouter-redir` + `fwrouter-tproxy`
- controller URL `http://127.0.0.1:5200`
