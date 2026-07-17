# `/opt/fwrouter-api/fwrouter_api/services/dataplane_nft.py`

## Назначение

Генерирует owned `nftables` table `inet fwrouter_v2` для global/scoped routing, shared destination sets, transparent steering и loop-prevention. После последнего фикса файл еще и держит secure-DNS bypass guard в `prerouting`, чтобы selective path не обходился через DoH/DoT.

## Важные функции

- `_build_scoped_vpn_sets()`
  Строит компактные scoped matcher sets для subject-level VPN path. Это подтверждает, что проект не генерирует отдельный полный ruleset на каждого клиента.

- `render_owned_table_candidate()`
  Главный renderer `inet fwrouter_v2`. Создает:
  - protected/direct/vpn persistent sets
  - DNS-runtime `dns_direct_ipv4`/`dns_vpn_ipv4` sets с timeout для `dnsmasq nftset` materialization
  - `fwrouter_classify`
  - `fwrouter_direct`
  - `fwrouter_vpn`
  - `fwrouter_vpn_full`
  - `prerouting` с UDP `tproxy`
  - `prerouting_nat` с TCP `redirect` в managed `fwrouter-redir`
  - `output_nat` остается как host-local redirect contour
  - `output_nat` с TCP `redirect` для local-origin traffic
  - secure DNS bypass drop rule
  - LAN-ingress IPv6 reject rule
  - early LAN DNS `53/tcp,udp` accept перед classify, чтобы iptables DNAT DNS capture успевал отправить DNS на router DNS даже для scoped VPN clients
  - per-subject steering rules и counters
  - per-subject accounting counters: `direct_tx` в terminal `fwrouter_direct`, `direct_rx` в `forward`, `vpn_tx` в terminal VPN chains, `vpn_rx` в `output` только для packets с proxy-bypass mark
  - использует только manifest data и static secure-DNS fallback; runtime Docker/DNS/system discovery внутри renderer больше запрещены

## Внешние зависимости

- `nft`
- manifest из apply pipeline
- effective rules artifact

## Runtime/persistent state

- пишет generated candidate/applied `nft` artifacts
- одинаковые JSON manifest artifacts (`job_candidate_manifest`, `snapshot_manifest`, `current/applied/last-good`) должны создаваться через atomic file copy от canonical JSON, а не повторной сериализацией одного и того же большого manifest
- формирует runtime-owned table `inet fwrouter_v2`
- не хранит persistent business state сам по себе

## Boot persistence relevance

Очень высокая. После reboot именно этот renderer восстанавливает:
- protected/private exclusions
- selective shared sets path
- TProxy steering
- scoped subject rules
- secure DNS bypass guard

## Нюансы

- `@vpn_ipv4/@direct_ipv4` shared для всех клиентов и хранят persistent IP/CIDR rules; per-client добавляются только короткие source-match classify rules
- `@dns_vpn_ipv4/@dns_direct_ipv4` — отдельные runtime sets для DNS materialization из `dnsmasq`; они имеют `timeout`, чтобы rotating CDN IP не копились бесконечно
- `fwrouter_classify` — decision chain: subject/global selective routing выбирает direct или vpn branch именно здесь
- `fwrouter_direct` — terminal direct path: direct counters + return, без `meta mark` и без `tproxy`
- `fwrouter_vpn` — terminal selective/domain-aware VPN path: VPN counters + marks для downstream `5202/5203` transparent contract
- `fwrouter_vpn_full` — terminal full-VPN path: VPN counters + marks для always-on `5204/5205` transparent contract
- `vpn_rx` counters должны матчить `meta mark 0x200 ip daddr <client>` в `output` перед `skip mihomo outbound recapture`; unmarked `ip daddr <client>` в `output` загрязняет VPN RX локальными/direct ответами роутера
- `direct_rx` counters остаются в `forward` по `ip daddr <client>` и отражают routed return traffic, а не local router output
- Для LAN/Tailscale selective ingress TCP handoff'ится через `prerouting_nat redirect to :5202` в managed `fwrouter-redir`; UDP остается на `tproxy to :5203`
- Для LAN/Tailscale full-VPN ingress TCP handoff'ится через `prerouting_nat redirect to :5204` в managed `fwrouter-full-redir`; UDP идет через `tproxy to :5205`
- TCP redirect использует отдельные internal marks `0x101`/`0x103`, чтобы не попасть под policy rules `fwmark 0x100/0x102 -> table 100`; `0x100`/`0x102` остаются UDP/TProxy marks
- diagnostics должны принимать `fwrouter redirect handoff tcp:5202` и `fwrouter full-vpn redirect handoff tcp:5204` как transparent TCP markers; `fwrouter tproxy handoff udp:5203` и `fwrouter full-vpn tproxy handoff udp:5205` остаются UDP markers
- если candidate реально использует `fwrouter_vpn`, renderer обязан неявно/явно нести contract marker `fwrouter vpn policy contract required v1`; это нужно для apply/check/rollback, особенно в `global=direct + scoped selective/vpn`
- active Xray `forced_vpn` subjects сами по себе не считаются причиной держать transparent LAN contract: renderer не должен выводить `vpn_policy_required=true` только из-за Xray runtime path
- `fwrouter_vpn` теперь еще и fast-fail отклоняет `udp/443` перед VPN-mark path, чтобы transparent web-клиенты быстрее откатывались с QUIC на TCP, когда selective VPN-path для браузера нестабилен
- shared interval sets рендерятся с `auto-merge`; persistent `@vpn_ipv4/@direct_ipv4` защищены от overlap между CIDR/IP rules, а DNS-runtime `@dns_vpn_ipv4/@dns_direct_ipv4` дополнительно имеют `timeout`
- `@secure_dns_bypass_ipv4` не должен разрастаться в бесконечный feed; это bounded operational guard, а не внешний ruleset
- bounded guard все же обязан включать реально встречающиеся browser/app DoH endpoints. Минимальный static baseline сейчас покрывает не только `1.1.1.1`/`8.8.8.8`, но и Cloudflare anycast `162.159.61.3/4` и `172.64.41.3/4`, иначе selective domain path у Android/mobile apps может тихо обходиться через encrypted DNS
- `manifest.extra.rules_effective_summary` не считается источником для `build_nft_rule_sets()`. Renderer использует `manifest.extra.rules_effective` только если там есть полный artifact с `rules`; иначе читает canonical effective-rules artifact напрямую
- manifest может дополнительно передавать `extra.infrastructure_ipv4` и `extra.secure_dns_bypass_ipv4`; это preferred input для renderer вместо runtime discovery
- `FWROUTER_DNSMASQ_NFTSET_TIMEOUT_SECONDS` управляет default TTL для DNS-runtime sets; persistent IP/CIDR rules этим timeout не затрагиваются
- immunity/protected/infrastructure guards в `prerouting` и `output` должны идти до subject/global capture, а не смешиваться с terminal direct branch semantics
- secure-DNS bypass guard должен жить до `jump fwrouter_classify`, иначе клиент сможет обойти domain-aware selective еще до materialization
- LAN-ingress IPv6 reject тоже должен жить до `jump fwrouter_classify`: это не selective decision, а hard ingress policy для внутреннего IPv4-only client contract
- LAN DNS capture accept тоже должен жить до `jump fwrouter_classify`; scoped `vpn` иначе перехватывает public DNS `:53` в transparent path до iptables nat PREROUTING
- protected/local destination guards в `fwrouter_classify` должны оставаться выше scoped `vpn` override, чтобы локальные/служебные сети не уходили в VPN даже в full VPN client mode
- guard deliberately uses fast-fail `reject`, not silent `drop`: TCP gets `reset`, UDP gets `icmpx port-unreachable`, чтобы клиенты быстрее откатывались с DoH/QUIC на обычный DNS и не висели на таймаутах
- по той же operational причине transparent VPN branch тоже fast-fail'ит `udp/443`, а не silently blackhole'ит QUIC retries
- значения `0x100/0x101/0x102/0x103/0x200`, `table 100`, `redir :5202/:5204`, `tproxy :5203/:5205` и `inet fwrouter_v2` остаются инвариантами
- comment-контракт `fwrouter vpn mark tcp:*`, `fwrouter vpn mark udp:*`, `fwrouter redirect handoff tcp:*`, `fwrouter tproxy handoff udp:*`, `fwrouter full-vpn redirect handoff tcp:*` и `fwrouter full-vpn tproxy handoff udp:*` считается runtime-инвариантом для LAN ingress
- `global_mode=direct` сам по себе больше не означает, что policy-routing можно удалить: если scoped rules still reach `fwrouter_vpn`, candidate должен сохранять marker/manifest requirement для `table 100`
