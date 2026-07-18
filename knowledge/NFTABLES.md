# NFTables

## Владение ruleset

Проект владеет собственной таблицей `inet fwrouter_v2`. Это сознательно изолирует логику `fwrouter` от системного/докерного firewall state.

## Основные владельцы логики

- `fwrouter_api/services/dataplane_global.py`
  описывает контракт и protected sets
- `fwrouter_api/services/dataplane_nft.py`
  рендерит owned candidate artifacts
- `/usr/local/libexec/fwrouter/dataplane-apply.sh`
  применяет candidate и проверяет chains
- `/usr/local/libexec/fwrouter/dataplane-check.sh`
  валидирует candidate/live state
- `/usr/local/libexec/fwrouter/dataplane-common.sh`
  общий POSIX helper для чтения manifest routing contract и совпадающей проверки policy-routing state
- `/usr/local/libexec/fwrouter/dataplane-rollback.sh`
  удаляет/replaces owned table через snapshot discipline

## Инварианты

- owned table должна быть безопасна к полному пересозданию
- повторный apply не должен ломать host networking
- проверка должна отличать `table missing after reboot` от реальной поломки кода
- `fwrouter` не должен переписывать чужие tables ради удобства
- `fwrouter_classify` должен держать protected/local exclusions (`fib daddr type local`, `@protected_ipv4`, `@protected_ipv6`) перед scoped `vpn` override; локальные/служебные адреса остаются direct даже при full VPN client mode
- быстрый `set_global_mode` может hot-swap'ить только `fwrouter_classify`, но не должен пересоздавать shared sets/counters: это сохраняет `dnsmasq nftset` runtime references и счетчики, пока live table/chains и policy-routing contract уже валидны. После hot-swap live chain обязан содержать expected comment markers из candidate; mismatch считается ошибкой apply path, а не cosmetic drift.
- одиночная смена LAN/Tailscale client mode (`direct/selective/vpn`) при `global=direct` использует тот же `fwrouter_classify` hot-swap, если live owned table/chains и VPN policy-routing contract уже готовы. Global/rules/server mutations остаются на full apply path.
- fast verify для client `direct` принимает explicit marker `scoped direct override: <subject_id>` либо clean fallback через `global direct v1`, но не должен пропускать stale subject markers от `vpn/selective`.

## Отношение к boot persistence

- live `nftables` state не считается persistent source of truth
- source of truth это SQLite intent плюс generated/last-good artifacts
- startup backend обязан восстановить live table, если после reboot она отсутствует

## Связь с transparent ingress

- transparent contour теперь split по semantics:
  - selective/domain-aware TCP ingress идет через `redirect to :5202`
  - selective/domain-aware UDP ingress идет через `tproxy to :5203`
  - full-VPN TCP ingress идет через `redirect to :5204`
  - full-VPN UDP ingress идет через `tproxy to :5205`
- `mihomo` transparent ingress должен совпадать с контрактом manifest
- `fwmark 0x100`, TCP redirect mark `0x101`, full-VPN UDP mark `0x102`, full-VPN TCP redirect mark `0x103` и bypass mark `0x200` должны оставаться согласованными между nftables, policy routing и generated `mihomo` config
- `0x100`/`0x102` используются только для TProxy/policy-routing path; TCP redirect использует `0x101`/`0x103`, чтобы не попадать в `table 100 local dev lo`
- обязательность policy-routing теперь определяется не только global mode, а явным manifest contract `summary.requires_vpn_policy_routing`; это критично для `global=direct + scoped selective/vpn`, где packets все равно могут идти в `fwrouter_vpn`
- `redirect` нужен именно для TCP transparent ingress; попытка тащить LAN TCP-path через тот же TProxy contour оказалась слишком хрупкой: live capture для `0.71 -> instagram` показывал клиентский SYN без SYN-ACK обратно
- `tproxy` остается только для UDP contour и policy-routing path `fwmark 0x100/0x102 -> table 100`
- shared destination sets `@direct_ipv4/@vpn_ipv4` объявляются как persistent interval sets с `auto-merge` для статических IP/CIDR rules. DNS materialization из `dnsmasq` идет в отдельные IPv4 timeout sets `@dns_direct_ipv4/@dns_vpn_ipv4`, чтобы rotating CDN IP не копились в live table бесконечно
- health `dnsmasq nftset` нельзя выводить только из того, что sets и конфиги существуют. Реальный контракт теперь включает active probe: локальный DNS resolve должен приводить к появлению возвращенных IPv4 в ожидаемом DNS-runtime set; если probe проваливается, `dnsmasq` подлежит restart even when config text is unchanged
- после restart `dnsmasq` active probe может кратко видеть stale/missing nftset materialization. `reconcile_dnsmasq_rules()` делает bounded retry только внутри текущего apply/reconcile после restart, чтобы transient DNS/nftset задержка не откатывала успешный nft apply и не оставляла client mode в `failed`.
- `runtime_convergence_scheduler` является быстрым self-heal слоем для этого контракта: при active global/scoped VPN/selective scope он периодически вызывает `reconcile_dnsmasq_rules()` и `reconcile_current_routing_if_drift()` под TTL. Daily `fwrouter-maintenance.timer` остается вторым слоем, но selective не должен ждать его при broken `dnsmasq nftset` runtime. `watchdog` только читает последний convergence status и не делает repair сам.
- для LAN operational contract проект теперь дополнительно режет `meta nfproto ipv6` на ingress LAN interface(s), которые dnsmasq already materialized as router-DNS bindings. Это deliberate решение: внутренние клиенты forced в IPv4-only path, while WAN-side/router-side IPv6 can still exist separately
- LAN DNS `53/tcp` и `53/udp` должен получать ранний `accept` в `prerouting` до `jump fwrouter_classify`; иначе scoped `vpn` перехватывает DNS в transparent path раньше, чем legacy iptables DNAT `fwrouter dns capture` успевает отправить запросы на router DNS
- secure DNS bypass guard в `prerouting` не должен использовать silent `drop` для TCP/UDP 443/853. Fast-fail `reject` сокращает браузерные/mobile таймауты при fallback с DoH/QUIC на обычный LAN DNS
- `manifest.extra.rules_effective_summary` считается только bounded summary для UI/debug; renderer `dataplane_nft.py` должен брать полный `rules_effective` только если в объекте есть реальные `rules`, иначе читать full effective-rules artifact из canonical source

## Traffic accounting counters

- `*_direct_tx` считается в terminal `fwrouter_direct` по source клиента.
- `*_direct_rx` считается в `forward` по destination клиента и отражает routed return traffic.
- `*_vpn_tx` считается в terminal VPN chain по source клиента.
- `*_vpn_rx` считается в `output` только с `meta mark 0x200` и destination клиента. Нельзя считать `vpn_rx` простым `ip daddr <client>` в `output`: так локальные/direct ответы роутера загрязняют VPN RX у direct-only клиентов.

## Риски

- потеря protected/private exclusions вызывает loop или перехват локального трафика
- изменение table/chains без обновления `dataplane-check.sh` сломает boot recovery
- попытка хранить live nft state как canonical config приведет к drift после reboot
- удаление `auto-merge` у interval set'ов ломает selective domain path: `dnsmasq` начинает получать `interval overlaps with an existing one`, после чего IP домена не материализуются в VPN/direct shared sets
- очистка `table 100` / `fwmark 0x100/0x102` только по `global_mode=direct` без учета scoped VPN path ломает transparent selective/full-VPN ingress при формально корректном ruleset
