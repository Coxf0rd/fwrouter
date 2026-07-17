# Troubleshooting

## Базовая проверка

```bash
/opt/fwrouter-api/scripts/check_boot_persistence.sh
systemctl --failed
journalctl -u fwrouter-api.service -u fwrouter-mihomo.service -u fwrouter-xray.service -u fwrouter-xray-sub-gateway.service -n 200 --no-pager
```

## Если странно работает DNS

```bash
host 2ip.ua 127.0.0.1
host 2ip.ua 1.1.1.1
cat /etc/resolv.conf
journalctl -u dnsmasq -n 100 --no-pager
iptables -t nat -L PREROUTING -v -n --line-numbers | sed -n '1,20p'
```

- если локальный `127.0.0.1` отдает `NXDOMAIN`, а `1.1.1.1` отвечает нормально, проблема в upstream resolver chain
- `dnsmasq` в этой схеме должен использовать public upstream из `fwrouter-upstream-dns.conf`, а не ISP DNS
- если в `journalctl -u dnsmasq` есть `nftset ... cache initialization failed: Ошибка протокола`, проверь формат `nftset` строк в `/etc/dnsmasq.d/fwrouter-rules.conf`
  - для live `dnsmasq 2.90` в текущем LAN IPv4-only contract нужен формат `nftset=/domain/4#inet#fwrouter_v2#dns_vpn_ipv4` или `#dns_direct_ipv4`
  - combined запись `nftset=/domain/4#...#set,6#...#set` может не материализовать set-и
  - runtime DNS IP должны попадать в timeout sets `dns_vpn_ipv4`/`dns_direct_ipv4`, а не в persistent `vpn_ipv4`/`direct_ipv4`
- если selective по доменам не срабатывает, проверь не только `dnsmasq`, но и DNS capture contract:
  - должны быть правила `fwrouter dns capture` для `53/tcp` и `53/udp` на LAN интерфейсе
  - в актуальной схеме это `DNAT --to-destination 192.168.0.1:53`, а не только `REDIRECT`
  - в `nft list chain inet fwrouter_v2 prerouting` должен быть ранний `allow LAN DNS capture before VPN classify`; без него scoped `vpn` перехватит DNS до iptables DNAT и Android/browser могут показать broken internet
  - без них клиент может игнорировать DHCP DNS и обходить `nftset` materialization
- отдельно проверь bypass по secure DNS:
  - `nft list chain inet fwrouter_v2 prerouting | grep 'block secure DNS bypass from LAN'`
  - если в capture у клиента видны успешные сессии на `8.8.8.8:443`, `8.8.4.4:443`, `162.159.61.3/4:443`, `172.64.41.3/4:443`, значит browser DoH path не закрыт и selective по доменам будет деградировать
  - актуальный static guard обязан покрывать как минимум `1.1.1.1/1.0.0.1`, `8.8.8.8/8.8.4.4`, `9.9.9.9`, `94.140.14.14/15`, `208.67.222.222/220.220`, а также Cloudflare DoH anycast `162.159.61.3/4` и `172.64.41.3/4`
- если у клиента в capture видно `192.168.x.x -> 1.1.1.1:53`, а counters `fwrouter dns capture` не растут, selective domain path фактически обходится

## Если не стартует Mihomo

- проверить `/dev/net/tun`
- проверить `docker ps`
- проверить `ss -ltnup | grep 5200`
- проверить generated `config.yaml`
- если контейнер в crash-loop, сразу смотреть `docker logs --tail 80 fwrouter-mihomo`; ошибка `unsupported rule type: SRC-IP` означает, что в `sub-rules["fwrouter-transparent"]` попал старый неподдерживаемый source rule. Должно быть `SRC-IP-CIDR,<client>/32,vpn-global`, не `SRC-IP,<client>,vpn-global`
- отдельно проверить transparent listener:
  - `grep -nA5 -B2 'fwrouter-tproxy' /var/lib/fwrouter-v2/generated/mihomo/config.yaml`
  - для `fwrouter-tproxy` bind должен быть `0.0.0.0`, не `127.0.0.1`
  - если explicit proxy `:5201` работает, а клиентский selective traffic не доходит до VPN-path, это первый подозреваемый

## Если не стартует Xray

- проверить `docker network inspect proxy_net`
- проверить `/var/lib/fwrouter-v2/xray/config.json`
- проверить логи контейнера и `/var/log/fwrouter/xray`

## Если backend поднялся, но routing не совпадает с intent

- смотреть `GET /api/v2/runtime`
- смотреть `GET /api/v2/routing/global`
- запустить `ip rule show`, `ip route show table all`, `nft list ruleset`
- проверить `applied-manifest.json` и last-good artifacts

Если `global mode = direct`, но в `ip rule show` все еще виден `fwmark 0x100 lookup fwrouter_vpn`, это не всегда stale contract: сначала проверьте, нет ли active scoped `vpn/selective` subject-ов, которым все еще нужен transparent VPN path.

Проверка:

```bash
ip rule show
ip route show table 100
```

Ожидаемо для pure `direct` без scoped VPN users:

- нет rule `priority 100 fwmark 0x100 lookup fwrouter_vpn`
- `table 100` пустая

Ожидаемо для `global=direct + scoped selective/vpn`:

- rule `priority 100 fwmark 0x100 lookup fwrouter_vpn` остается на месте
- `table 100` содержит `local default dev lo`

Если client API показывает `effective_mode=selective`, но `effective_state.dataplane_path=direct` или `scoped_runtime.status=not_applicable`, это не dataplane TCP problem. Проверяй `subject_policy`: per-client LAN/Tailscale selective не должен демотиться в direct из-за degraded global/domain selective support.

Если client API уже показывает `dataplane_path=selective`, но live `fwrouter_classify` не содержит строк вида `scoped selective ... <subject_id>`, это live nft drift. Проверка:

```bash
curl -sS -H 'Authorization: Bearer dev-secret' \
  'http://127.0.0.1:5000/api/v2/subjects/lan%3Afc-41-16-df-f3-5e'
nft list chain inet fwrouter_v2 fwrouter_classify
sqlite3 /var/lib/fwrouter-v2/fwrouter.db \
  "select job_id, requested_by, json_extract(input_json,'$.intent'), status, created_at from jobs order by created_at desc limit 5;"
```

Ожидаемое восстановление после backend restart: job с `requested_by=startup-scoped-subject-recovery` и intent `set_subject_admin_mode`, после чего live chain снова содержит scoped subject rules.

## Если UI или API стали медленными

Быстрые замеры:

```bash
curl -fsS -o /dev/null -w 'start=%{time_starttransfer} total=%{time_total}\n' http://127.0.0.1:5000/api/v2/runtime
curl -fsS -o /dev/null -w 'start=%{time_starttransfer} total=%{time_total}\n' http://127.0.0.1:5000/api/v2/system/summary
curl -fsS -o /dev/null -w 'start=%{time_starttransfer} total=%{time_total}\n' http://127.0.0.1:5000/api/v2/ui/clients
curl -fsS -o /dev/null -w 'start=%{time_starttransfer} total=%{time_total}\n' http://127.0.0.1:5000/api/v2/ui/whoami
curl -fsS -o /dev/null -w 'start=%{time_starttransfer} total=%{time_total}\n' http://127.0.0.1:5000/api/v2/ui/settings/workspace
curl -fsS -o /dev/null -w 'start=%{time_starttransfer} total=%{time_total}\n' 'http://127.0.0.1:5000/api/v2/ui/settings/inventory?kind=xray&limit=200'
```

- первый запрос может быть медленным из-за cold cache; второй подряд должен быть заметно быстрее
- если `runtime` медленный и повторный запрос не ускоряется, сначала проверять:
  - `fwrouter_api/services/live_probe_cache.py`
  - `fwrouter_api/services/dataplane_status.py`
  - `fwrouter_api/adapters/mihomo.py`
- если медленный именно первый запрос после startup/apply:
  - проверить, успел ли закончиться async prewarm read-model cache
  - помнить, что `/runtime` и `/ui/settings/workspace` все равно могут быть тяжелыми, если live diagnostics еще не прогреты
- если медленный именно cold `/runtime`, типовые hotspots сейчас такие:
  - `dataplane_check` script
  - `build_runtime_enforcement_state()`
  - `list_subjects_effective_summaries()`
  - duplicate Mihomo health probes, если сломался shared cache key между `runtime.py` и `dataplane_global.py`
- если `ui/clients` медленный, обычно hotspot в:
  - `traffic_monthly` aggregation
  - `list_subjects_with_effective_state()`
- user view не должен дергать `ui/clients` ради текущего клиента. Для этого есть `ui/whoami`, который определяет LAN/Tailscale subject по IP и возвращает `effective_state`.
- admin/settings списки должны использовать lightweight `ui/settings/inventory?kind=...`; если они снова ждут `ui/clients`, это regression frontend read-path.
- если `ui/settings/workspace` медленный, смотреть:
  - `get_xray_status()`
  - recent logs loading
  - workspace cache в `ui_state.py`

Проверка maintenance/retention:

```bash
systemctl status --no-pager fwrouter-maintenance.timer fwrouter-traffic-collect.timer
/opt/fwrouter-api/.venv/bin/python - <<'PY'
from fwrouter_api.services.maintenance import run_control_plane_maintenance
print(run_control_plane_maintenance(dry_run=True))
PY
```

- если `log_dir` и `state_dir` быстро растут, сначала запускать штатный maintenance, а не писать ad-hoc cleanup scripts

## Если после reboot пропал dataplane

- это ожидаемый класс сбоя, startup recovery должен его восстановить
- проверить journald backend startup и `bootstrap` events
- проверить, что `fwrouter-api.service` стартовал после Mihomo/Xray и preflight не падал
## `dnsmasq` пишет `nftset ... interval overlaps with an existing one`

Признак:
- selective-клиент резолвит домен, но VPN/direct path по домену не срабатывает
- в `journalctl -u dnsmasq` есть ошибки `nftset inet fwrouter_v2 dns_vpn_ipv4 Error: interval overlaps with an existing one`

Причина:
- DNS-runtime `nft` sets (`@dns_vpn_ipv4/@dns_direct_ipv4`) или persistent sets были объявлены как `flags interval`, но без `auto-merge`
- когда `dnsmasq` пытался добавить runtime IP домена, перекрывающий уже существующий CIDR, netlink update отклонялся

Что проверить:
```bash
nft list set inet fwrouter_v2 dns_vpn_ipv4 | sed -n '1,12p'
journalctl -u dnsmasq -n 50 --no-pager
host instagram.com 127.0.0.1
```

Ожидаемое состояние:
- в DNS-runtime set видно `flags interval, timeout`, отдельную строку `auto-merge` и `timeout ...s`
- после новых DNS-резолвов больше нет свежих `interval overlaps`

## Телефон/браузер висит на `2ip`/`instagram`, хотя selective/direct формально применен

Признак:
- `fwrouter_vpn` counters растут
- `dnsmasq` уже не пишет `interval overlaps`
- но клиент долго висит на открытии сайтов, особенно если пытается использовать Secure DNS / DoH / QUIC

Причина:
- silent `drop` для `443/853` к `@secure_dns_bypass_ipv4` заставляет клиента ждать таймаутов на `dns.google` / Cloudflare DoH / QUIC endpoints

Ожидаемый контракт:
- TCP bypass попытки должны получать быстрый `reject with tcp reset`
- UDP bypass попытки должны получать быстрый `reject with icmpx type port-unreachable`
- после этого клиент быстрее откатывается на обычный DNS через роутер и перестает подвисать на retry-timeouts

## Selective packet доходит до `fwrouter_vpn`, но `mihomo` не видит transparent connections

Признак:
- `nftrace` показывает `fwrouter_vpn -> mark 0x101 -> prerouting_nat redirect` для TCP или `fwrouter_vpn -> mark 0x100 -> prerouting tproxy accept` для UDP
- `vpn_tx` counters растут
- но `mihomo` не показывает transparent client connections, а клиентский трафик все равно выглядит как broken

Причина:
- `fwrouter-tproxy` может быть loopback-bound или вообще отсутствовать, даже если config формально выглядит живым
- отдельная ошибка: TCP handoff через `tproxy :5203` может не materialize как TCP session; live capture для `0.71 -> instagram` показывал SYN без SYN-ACK обратно

Правильный контракт:
- `fwrouter-tproxy` должен быть bound на documented wildcard `0.0.0.0`, а не на `127.0.0.1`
- LAN/Tailscale TCP ingress должен handoff'иться через `redirect to :5202`, UDP через `tproxy to :5203`
- смотреть не только listener `:5202`, но и live counters:
  - `fwrouter vpn mark tcp:5202`
  - `fwrouter vpn mark udp:5203`
  - `fwrouter redirect handoff tcp:5202`
  - `fwrouter tproxy handoff udp:5203`
- если mark counters растут, а handoff counters нет, проблема в handoff между `fwrouter_vpn` и соответствующим ingress hook: `prerouting_nat redirect` для TCP или `prerouting tproxy` для UDP
- если handoff counters растут, а transparent сессий все равно нет, проблема уже внутри `mihomo` transparent ingress contract

## `instagram`/`google generate_204` через selective висят, но explicit proxy `:5201` работает

Признак:
- `curl -x http://127.0.0.1:5201 https://www.instagram.com` отвечает
- `fwrouter_vpn` counters у клиента растут
- direct сайты открываются, а selective browser-traffic висит или телефон показывает `нет интернета`

Вероятная причина:
- браузер/Android упирается в `QUIC` (`udp/443`) поверх transparent selective VPN-path, а не в обычный TCP

Ожидаемый контракт:
- в `chain fwrouter_vpn` должно быть fast-fail правило `udp dport 443 reject with icmpx type port-unreachable`
- после этого web-клиент быстрее откатывается на TCP/TLS и перестает зависать на QUIC retry loop

Проверка:
```bash
nft list chain inet fwrouter_v2 fwrouter_vpn | grep 'force transparent web clients off QUIC onto TCP'
curl -I -m 12 -x http://127.0.0.1:5201 https://www.instagram.com
curl -I -m 12 -x http://127.0.0.1:5201 https://www.google.com/generate_204
```
