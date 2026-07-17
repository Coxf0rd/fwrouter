# `/usr/local/libexec/fwrouter/dataplane-apply.sh`

## Назначение

Применяет owned `nftables` table и policy routing contract по generated candidate/manifest.

## Важные функции

Shell script sources shared helper `/usr/local/libexec/fwrouter/dataplane-common.sh`; apply-specific логика остается линейной:

- валидация входных файлов
- чтение contract из manifest через общий helper с fallback по `vpn_contour` / `dataplane_profile.vpn_routing_contract` / `global_preflight.vpn_contour`
- решение `ensure_policy_routing` vs `cleanup_policy_routing` теперь опирается на manifest-first flag `summary.requires_vpn_policy_routing`, а не только на global mode
- удаление старой `fwrouter_v2` table
- loop-delete legacy `ip rule` для `0x100`, `0x102` и `0x200`
- если VPN policy required: `ip route replace local default dev lo table 100` и идемпотентные `ip rule add fwmark 0x100/0x102 ...`
- если VPN policy required и доступна утилита `conntrack`: после успешной установки nft/policy routing удаляет старые IPv4 conntrack flows с source из клиентских ranges `10/8`, `100.64/10`, `172.16/12`, `192.168/16`.
  Это заставляет новые TCP connections заново пройти NAT redirect/tproxy contract после смены или восстановления dataplane.
- если VPN policy not required: cleanup policy routing contract, чтобы не оставлять stale `fwmark 0x100/0x102`/`table 100`
- применение candidate через `nft -f`

## Внешние зависимости

- `nft`
- `ip`
- `conntrack` optional; отсутствие не считается ошибкой
- JSON manifest
- snapshot/last-good files

## Runtime/persistent state

- меняет live kernel dataplane
- может писать snapshots/result payloads

## Boot persistence relevance

Критическая.

## Нюансы

- удаление старых правил до add обеспечивает идемпотентность; сами `ip rule add` не должны hard-fail на `RTNETLINK answers: File exists`, потому что финальная гарантия проверяется отдельным `policy_routing_ready`
- нельзя менять marks/table id только в одном месте
- result payload `required_chains` должен включать полный runtime contract, включая `input` и `fwrouter_vpn_full`; иначе Python runtime diagnostics может ложно считать live table отсутствующей или принять старую неполную таблицу за готовую
- `global=direct` и одновременно active scoped selective/vpn больше не считается поводом для unconditional cleanup: apply обязан сохранить transparent routing contract, если manifest этого требует
- no-op validate path не чистит kernel state сам по себе; для устранения stale `fwmark` при уже активном `direct` нужен runtime repair/apply path
- conntrack cleanup должен оставаться bounded и apply-time only; не добавлять фоновый polling/flush loop
