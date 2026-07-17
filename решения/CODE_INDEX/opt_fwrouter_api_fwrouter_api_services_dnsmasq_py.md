# `/opt/fwrouter-api/fwrouter_api/services/dnsmasq.py`

## Назначение

Генерирует `dnsmasq`-конфиги для domain-aware selective routing, при необходимости перезапускает `dnsmasq` и теперь же обеспечивает обязательный LAN DNS capture через `iptables-nft` nat `PREROUTING`.

## Важные функции

- `_discover_router_dns_ipv4_addresses()`
  Ищет приватные IPv4 адреса роутера на невиртуальных интерфейсах.

- `_discover_router_dns_bindings()` / `_discover_router_dns_interfaces()`
  Строят binding-модель DNS-роутера по интерфейсам. Это важно, потому что DNS capture ставится не глобально по всем private source, а адресно по найденным LAN интерфейсам.

- `inspect_dns_capture_status()`
  Проверяет, стоят ли `iptables -t nat PREROUTING ... DNAT --to-destination <router-ip>:53` правила с комментарием `fwrouter dns capture`.

- `ensure_dns_capture_rules()`
  Идемпотентно вставляет TCP/UDP DNAT на `<router-ip>:53` в `PREROUTING` для найденных LAN интерфейсов. После практической диагностики это оказалось надежнее, чем `REDIRECT`, для реального перехвата клиентских запросов к `1.1.1.1:53`.

- `inspect_dnsmasq_selective_status()`
  Проверяет наличие managed конфигов, соответствие forced DHCP DNS contract и наличие реального DNS capture. После фикса это главный guard против ложного `domain-aware selective ready`, когда клиент игнорирует DHCP DNS и ходит на `1.1.1.1:53` напрямую.
  Дополнительно делает bounded active probe: резолвит по одному `VPN` и `DIRECT` домену через локальный `dnsmasq` и проверяет, что полученные IPv4 реально materialize в соответствующие DNS-runtime `nft` sets.

- `reconcile_dnsmasq_rules()`
  Генерирует:
  - `/etc/dnsmasq.d/fwrouter-rules.conf`
  - `/etc/dnsmasq.d/fwrouter-dhcp-dns.conf`
  - `/etc/dnsmasq.d/fwrouter-ipv6-lan.conf`
  - `/etc/dnsmasq.d/fwrouter-local-hosts.conf`
  И перезапускает `dnsmasq`, если конфиги реально изменились, либо если active probe показывает broken `nftset` runtime при неизменном конфиге.
  Параллельно приводит в норму `iptables` DNS capture contract даже если тексты конфигов не менялись.
  Для domain-aware selective пишет IPv4-only `nftset` строки в `dns_vpn_ipv4`/`dns_direct_ipv4`. `dnsmasq` больше не пишет runtime DNS IP в persistent `vpn_ipv4`/`direct_ipv4`: эти sets остаются для статических IP/CIDR rules, а DNS-результаты живут в timeout sets.
  Для LAN IPv4-only contract еще и гарантирует `filter-aaaa`, чтобы локальные DNS клиенты не получали AAAA ответы.
  Для локального LAN ingress генерирует `address=/fwrouter.lan/<router-ip>` и `address=/homes.lan/<router-ip>`; сами HTTP upstreams обслуживаются Nginx Proxy Manager, не `dnsmasq`.
  Отдельно встраивает built-in direct nftset bindings для Android connectivity-check domains, чтобы индикатор интернет-доступа не зависел от широких VPN feeds и IP overlap.

## Внешние зависимости

- `systemctl restart dnsmasq`
- `ip -json address show`
- `iptables -t nat`
- effective rules artifact
- owned nft table name

## Runtime/persistent state

- пишет persistent dnsmasq config fragments в `/etc/dnsmasq.d/`
- создает runtime `iptables` nat DNAT rules для `53/tcp` и `53/udp`

## Boot persistence relevance

Высокая для domain-aware selective path. После reboot именно `reconcile_dnsmasq_rules()` должен вернуть не только managed конфиги, но и DNS capture rules, иначе selective по доменам может silently деградировать.

## Нюансы

- защищенные service domains специально исключаются
- перезапуск `dnsmasq` должен происходить только при реальном изменении конфигов
- `dhcp-option-force` сам по себе не гарантирует domain-aware selective: клиенты могут игнорировать DHCP DNS
- для этого проект теперь дополнительно полагается на `iptables-nft` DNAT в `PREROUTING`
- readiness selective нельзя снова сводить только к наличию `fwrouter-rules.conf` и `fwrouter-dhcp-dns.conf`
- combined `nftset=/domain/4#...#v4,6#...#v6` оказался несовместим с live `dnsmasq 2.90`; текущий LAN contract IPv4-only, поэтому managed rules используют только `4#inet#fwrouter_v2#dns_vpn_ipv4` / `4#inet#fwrouter_v2#dns_direct_ipv4`
- DNS-runtime sets должны быть timeout sets: CDN/rotating DNS ответы не должны бесконечно раздувать persistent routing sets
- broken runtime state `dnsmasq` возможен и без изменения файлов конфигурации; поэтому reconcile обязан уметь self-heal через restart по результату active `nftset` probe, а не только по diff текста
- `filter-aaaa` здесь используется как operational LAN policy, а не как глобальный feature-toggle для всего проекта: текущая цель именно не давать LAN clients usable IPv6 DNS answers
