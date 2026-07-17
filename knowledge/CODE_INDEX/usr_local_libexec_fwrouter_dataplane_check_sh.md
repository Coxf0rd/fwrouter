# `/usr/local/libexec/fwrouter/dataplane-check.sh`

## Назначение

Проверяет корректность candidate/live dataplane и возвращает structured JSON для backend.

## Важные функции

- `nft -c -f` candidate validation
- shared helper `/usr/local/libexec/fwrouter/dataplane-common.sh` materializes manifest routing contract with the same fallback order as apply
- live table presence check
- required chains check
- required chains теперь включают полный runtime contract: `prerouting`, `input`, `output`, `forward`, `postrouting`, `fwrouter_classify`, `fwrouter_direct`, `fwrouter_vpn`, `fwrouter_vpn_full`
- verification `ip rule`/`ip route` contract теперь привязана к resolved `vpn_policy_required`, а не только к `routing_mode in {vpn, selective}`

## Внешние зависимости

- `nft`
- `ip`
- generated manifest

## Runtime/persistent state

- read-only по отношению к системе

## Boot persistence relevance

Критическая. Startup recovery опирается на результат этого скрипта.

## Нюансы

- manifest-first flag `summary.requires_vpn_policy_routing` является canonical source of truth; grep по comment-marker в candidate/live table нужен как fallback для mixed-era artifacts
- `global=direct + scoped selective/vpn` должен считаться VPN-contract-required сценарием, если packets реально могут дойти до `fwrouter_vpn`
- candidate validation теперь ожидает split transparent contract:
  - `fwrouter redirect handoff tcp:<redir-port>`
  - `fwrouter tproxy handoff udp:<tproxy-port>`
  - `fwrouter full-vpn redirect handoff tcp:<full-redir-port>`
  - `fwrouter full-vpn tproxy handoff udp:<full-tproxy-port>`
  - `redirect to :<redir-port>` для TCP
  - `tproxy to :<tproxy-port>` для UDP
- `policy_routing_ready` проверяет оба UDP/TProxy marks: `0x100` для selective и `0x102` для full-VPN
