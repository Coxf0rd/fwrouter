# Boot Flow

## Какие сервисы должны быть enabled

- `fwrouter-mihomo.service`
- `fwrouter-xray.service`
- `fwrouter-api.service`
- `fwrouter-xray-sub-gateway.service`
- `fwrouter-subscription-refresh.timer`
- `fwrouter-maintenance.timer`
- `fwrouter-jobs-retention-dry-run.timer`
- `fwrouter-traffic-collect.timer`

## Порядок старта

1. `network-online.target` и `docker.service`
2. `fwrouter-mihomo.service`
3. `fwrouter-xray.service`
4. `fwrouter-api.service`
5. `fwrouter-xray-sub-gateway.service`
6. timers и регулярные jobs

Фактическая защита от race conditions:

- `fwrouter-api.service` имеет `After/Wants` на `fwrouter-mihomo.service` и `fwrouter-xray.service`
- Mihomo startup ждет `127.0.0.1:5200`
- gateway ждет `127.0.0.1:5000`
- Xray startup требует `docker network inspect proxy_net`

## Что должно быть готово до старта backend

- `/dev/net/tun`
- команды `nft` и `ip`
- Docker daemon
- persistent dirs и runtime dirs
- `sysctl` с `src_valid_mark=1`, `ip_forward=1`, `rp_filter=0`
- routing table alias `100 fwrouter_vpn`

## Что backend должен создать сам

- `/var/lib/fwrouter-v2/*` bootstrap directories
- `/var/log/fwrouter/*`
- `/run/fwrouter-v2`
- live owned `nftables` table и `ip rule/ip route`, если после reboot они отсутствуют
- Mihomo selector restore и intended routing recovery
- scoped LAN/Tailscale subject rules, если SQLite intent говорит `direct/selective/vpn`, а live `fwrouter_classify` не содержит subject-specific rules

## Что не переживает reboot

- `nftables` table `inet fwrouter_v2`, если не поднимается отдельным системным firewall unit
- `ip rule`
- `ip route` в custom table
- live listener/process state контейнеров
- соответствие live `fwrouter_classify` persisted per-client overrides после ручного/аварийного reload dataplane

## Что должно быть persistent

- `/etc/sysctl.d/99-fwrouter-routing.conf`
- `/etc/iproute2/rt_tables.d/fwrouter.conf`
- systemd units и timers
- generated configs в `/var/lib/fwrouter-v2/generated/`
- last-good snapshots и SQLite state

## Директории

Создаются заранее или через bootstrap:

- `/var/lib/fwrouter-v2`
- `/var/lib/fwrouter-v2/generated`
- `/var/lib/fwrouter-v2/jobs`
- `/var/lib/fwrouter-v2/cache`
- `/var/lib/fwrouter-v2/state`
- `/var/log/fwrouter`

Можно считать runtime-only:

- `/run/fwrouter-v2`
- debug dumps внутри `/var/lib/fwrouter-v2/debug`

## Как проверить подъем после reboot

```bash
systemctl status --no-pager fwrouter-mihomo.service fwrouter-xray.service fwrouter-api.service fwrouter-xray-sub-gateway.service
systemctl is-enabled fwrouter-mihomo.service fwrouter-xray.service fwrouter-api.service fwrouter-xray-sub-gateway.service
systemctl is-enabled fwrouter-subscription-refresh.timer fwrouter-maintenance.timer fwrouter-jobs-retention-dry-run.timer fwrouter-traffic-collect.timer
ip rule show
ip route show table all
nft list ruleset
sysctl net.ipv4.ip_forward net.ipv4.conf.all.src_valid_mark net.ipv4.conf.all.rp_filter net.ipv4.conf.default.rp_filter
ss -ltnup | grep -E '127.0.0.1:5000|127.0.0.1:5200|:5202|:5055'
/opt/fwrouter-api/scripts/check_boot_persistence.sh
```

## Если после backend restart пропали scoped client rules

Сценарий: SQLite/API показывают client `desired_mode=selective` или `vpn`, но live chain содержит только `goto fwrouter_direct comment "global direct v1"`.

Ожидаемая защита:

- `bootstrap.recover_startup_scoped_subject_routing()` читает active LAN/Tailscale subjects из SQLite
- затем читает `nft list chain inet fwrouter_v2 fwrouter_classify`
- если persisted scoped subject отсутствует в live chain, backend запускает обычный `set_subject_admin_mode(..., requested_by="startup-scoped-subject-recovery")`
- re-apply должен пересобрать subject-aware manifest; нельзя считать `global=direct` pure-direct runtime, пока есть per-client `selective`/`vpn`
