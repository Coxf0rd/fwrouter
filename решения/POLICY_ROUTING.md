# Policy Routing

## Канонический контракт

- routing table name: `fwrouter_vpn`
- routing table id: `100`
- `ip rule priority`: `100`
- primary fwmark: `0x00000100` (`256`)
- full-VPN UDP fwmark: `0x00000102` (`258`)
- bypass mark: `0x00000200` (`512`)
- route target: `local default dev lo`
- split transparent ingress:
  - selective TCP: `redir-port 5202`
  - selective UDP: `tproxy-port 5203`
  - full-VPN TCP: `redir-port 5204`
  - full-VPN UDP: `tproxy-port 5205`

## Где это реализовано

- `/etc/iproute2/rt_tables.d/fwrouter.conf`
- `fwrouter_api/services/dataplane_global.py`
- `/usr/local/libexec/fwrouter/dataplane-apply.sh`
- `/usr/local/libexec/fwrouter/dataplane-check.sh`
- `/usr/local/libexec/fwrouter/dataplane-common.sh`
- `/usr/local/libexec/fwrouter/dataplane-rollback.sh`

## Boot persistence

- `rt_tables.d` файл persistent
- live `ip rule` и `ip route` не persistent и должны пересоздаваться после reboot
- recovery запускается через backend startup

## Идемпотентность

`dataplane-apply.sh` и `dataplane-rollback.sh` loop-delete legacy rules и потом делают `ip route replace`, а не `add` вслепую. Это нельзя ломать.

`dataplane-apply.sh` и `dataplane-check.sh` должны читать manifest routing contract через один и тот же shared helper/fallback order, иначе apply/check начнут расходиться на mixed-era manifest artifacts.

При переходе в `direct` policy-routing контракт должен быть очищен только если candidate реально не использует transparent VPN path:

- если `summary.requires_vpn_policy_routing=false`, rules `priority 100 fwmark 0x100 lookup fwrouter_vpn` и `priority 101 fwmark 0x102 lookup fwrouter_vpn` должны исчезать;
- если `summary.requires_vpn_policy_routing=false`, `ip route show table 100` не должен оставлять `local default dev lo`;
- если `global_mode=direct`, но есть active scoped `vpn` / scoped `selective`, который реально может привести пакет в `fwrouter_vpn`, policy-routing контракт обязан остаться поднятым.

## Что проверять

```bash
ip rule show
ip route show table 100
grep -R . /etc/iproute2/rt_tables.d
```

Ожидаемое состояние:

- в `selective` и `vpn` есть `fwmark 0x100 lookup fwrouter_vpn`, `fwmark 0x102 lookup fwrouter_vpn` и `local default dev lo`;
- в `direct` их быть не должно только для pure direct candidate;
- в `direct + scoped selective/vpn` они должны оставаться, если manifest требует VPN policy routing.

Важно:
- после split transparent contour `table 100` нужен в первую очередь для UDP/TProxy path;
- TCP transparent ingress больше не должен зависеть от `tproxy-port`, он идет через `redirect to :5202` для selective и `redirect to :5204` для full-VPN.
- TCP redirect packets используют отдельные marks `0x101`/`0x103`; policy rules должны матчить только UDP/TProxy marks `0x100`/`0x102`, иначе TCP redirect может уйти в TProxy local route вместо нормальной redir-сессии.

## Риски

- изменение table id ломает contract с generated artifacts и runtime checks
- дубли `ip rule` ведут к непредсказуемому traffic steering
- потеря `src_valid_mark=1` ломает marked routing semantics
