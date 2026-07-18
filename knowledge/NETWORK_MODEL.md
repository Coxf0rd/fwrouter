# Network Model

## Общая модель

Хост использует `mihomo` как основной transparent egress dataplane. Control-plane держит intended routing state в SQLite и materializes его в:

- `nftables` table `inet fwrouter_v2`
- fwmark-based `ip rule`
- routing table `100 fwrouter_vpn`
- generated Mihomo config с `tproxy` listener и selector groups

## Интерфейсы и listeners

- `127.0.0.1:5000` `fwrouter-api`
- `127.0.0.1:5200` Mihomo controller
- `127.0.0.1:5201` Mihomo mixed proxy contour
- `0.0.0.0:5202` Mihomo transparent `tproxy` listener
- `172.18.0.1:5055` xray subscription gateway
- `/dev/net/tun` проброшен в Mihomo container
- важный инвариант:
  - `fwrouter-mixed` может быть loopback-only
  - `fwrouter-tproxy` не должен быть `127.0.0.1`-bound; для transparent interception нужен IPv4 wildcard bind `0.0.0.0`

## TUN и TProxy

- TUN нужен Mihomo контейнеру и проверяется в boot preflight.
- Основной VPN steering contract строится как `tproxy` contour.
- `build_vpn_steering_contract()` задает:
  - `fwmark_hex = 0x00000100`
  - `fwmark_value = 256`
  - `proxy_bypass_mark_hex = 0x00000200`
  - `proxy_bypass_mark_value = 512`
  - `routing_table_id = 100`
  - `ip_rule_priority = 100`
  - `selector_name = vpn-global`

## Routing tables и marks

- таблица `100 fwrouter_vpn`
- route target: `local default dev lo`
- mark `0x100` направляет трафик в таблицу `100`
- mark `0x200` используется как proxy bypass mark и не должен теряться в generated configs

## Protected addresses и loop prevention

Защищенные IPv4 сети:

- `127.0.0.0/8`
- `10.0.0.0/8`
- `172.16.0.0/12`
- `192.168.0.0/16`
- `100.64.0.0/10`
- `169.254.0.0/16`
- `224.0.0.0/4`

Защищенные IPv6 сети:

- `::1/128`
- `fc00::/7`
- `fe80::/10`
- `ff00::/8`

Защищенные service domains:

- `localhost`
- `tailscale.com`
- `dl.tailscale.com`
- `pkgs.tailscale.com`
- `vpn.minisk.ru`

Их нельзя менять без понимания loop-prevention и service reachability.

## DNS

- backend интегрируется с `dnsmasq` через `services/dnsmasq.py`
- domain-selective enforcement в runtime явно помечен как ограниченный контракт
- selective path может быть `ip_only` или `domain_aware`
- LAN-клиенты в текущем operational contract считаются IPv4-only: роутер не должен раздавать им usable IPv6 path для web/app traffic
- для `domain_aware` недостаточно только `dhcp-option-force`; клиент может игнорировать DHCP DNS и спрашивать `1.1.1.1:53` напрямую
- поэтому актуальный контракт такой:
  - `dnsmasq` публикует managed `server=/.../1.1.1.1` и IPv4-only `nftset=/.../dns_vpn_ipv4|dns_direct_ipv4`
  - Android connectivity-check domains (`connectivitycheck.gstatic.com`, `connectivitycheck.android.com`, `clients3.google.com`, `clients.l.google.com`, `www.google.com`, `www.gstatic.com`) встроенно forced в `direct`, даже если широкий VPN feed покрывает их по домену или IP overlap
  - LAN DHCP должен раздавать клиентам только router DNS (`192.168.0.1`); внешний secondary DNS ломает domain-aware selective, потому что dnsmasq не видит запросы и не материализует nft sets для connectivity-check/domain rules
  - `dnsmasq` DHCP fragment `/etc/dnsmasq.d/fwrouter-dhcp-dns.conf` форсирует DNS роутера; старые DNS DHCP options в `/etc/dnsmasq.conf` и `/etc/dnsmasq.d/lan.conf` должны оставаться закомментированы, иначе `dnsmasq` пишет `Ignoring duplicate dhcp-option 6`
  - `dnsmasq` managed fragment `fwrouter-ipv6-lan.conf` включает `filter-aaaa`, чтобы LAN-клиенты не получали AAAA answers от локального DNS
  - `dnsmasq` managed fragment `fwrouter-local-hosts.conf` публикует LAN-only hostnames `fwrouter.lan` и `homes.lan` на IPv4 адрес роутера; HTTP routing по этим именам выполняет Nginx Proxy Manager на `:80`
  - `services/dnsmasq.py` дополнительно держит `iptables -t nat PREROUTING DNAT --to-destination <router-ip>:53` на LAN интерфейсах
- runtime-convergence scheduler регулярно проверяет active VPN/selective scope: `dnsmasq` nftset materialization ремонтируется через `reconcile_dnsmasq_rules()`, а live `inet fwrouter_v2` marker drift — через `reconcile_current_routing_if_drift()`. Это быстрый слой самовосстановления до ежедневного maintenance. Watchdog использует только read-only status этого слоя, чтобы не принимать failover-knowledge по недостоверному runtime.
- `services/dataplane_nft.py` дополнительно держит в `inet fwrouter_v2 prerouting` drop-правило для known secure-DNS bypass endpoints:
  - `@secure_dns_bypass_ipv4`
  - `tcp/udp 443`
  - `tcp/udp 853`
- тот же renderer теперь ставит early `ipv6` reject на LAN ingress интерфейсах, известных из `dnsmasq_selective_status.router_dns_interfaces`; это operational stop-gap, который держит LAN client path strictly IPv4-only even if клиент сам знает AAAA или пытается обойти локальный DNS
- это нужно, чтобы браузерный DoH/DoT не обходил `dnsmasq` и не ломал materialization `@dns_vpn_ipv4/@dns_direct_ipv4`
- именно этот DNS capture нужен, чтобы DNS-runtime `@dns_vpn_ipv4/@dns_direct_ipv4` materialize'ились даже если клиент пытается уйти на внешний DNS напрямую
- static IP/CIDR rules остаются в persistent `@vpn_ipv4/@direct_ipv4`; DNS-runtime sets имеют timeout и не должны расти бесконечно из-за rotating CDN answers

## Local LAN ingress names

- `fwrouter.lan` резолвится локальным `dnsmasq` в `192.168.0.1` и проксируется через Nginx Proxy Manager на host nginx `192.168.0.1:5500`.
- `homes.lan` резолвится локальным `dnsmasq` в `192.168.0.1` и проксируется через Nginx Proxy Manager на локальный сервис `192.168.0.1:8123`.
- прямой доступ остается доступен: `http://192.168.0.1:5500/` для FWRouter UI и `http://192.168.0.1:8123/` для локального сервиса.

## Как traffic попадает в proxy

1. control-plane генерирует desired nft rules и Mihomo config.
2. `dataplane-apply.sh` ставит fwmark и policy routing.
3. marked traffic маршрутизируется в local route table `100`.
4. Mihomo transparent listener принимает трафик на `tproxy_port`.
5. selector `vpn-global` направляет egress либо на fixed server, либо на `vpn-auto`.

## Что особенно опасно менять

- значения fwmark `0x100/0x200`
- table id `100`
- priority `100`
- protected networks/domains
- имя owned table `inet fwrouter_v2`
- port contract `5000/5200/5201/5202/5055`
- transparent bind contract:
  - `5202` должен оставаться transparent listener с bind `0.0.0.0`
  - нельзя “оптимизировать” его до `127.0.0.1`, даже если explicit proxy contour продолжит работать
