# Configs And State

## Persistent config

- `/etc/systemd/system/fwrouter-*.service`
- `/etc/systemd/system/fwrouter-*.timer`
- `/etc/sysctl.d/99-fwrouter-routing.conf`
- `/etc/iproute2/rt_tables.d/fwrouter.conf`
- `/etc/dnsmasq.d/fwrouter-rules.conf`
- `/etc/dnsmasq.d/fwrouter-dhcp-dns.conf`
- `/etc/dnsmasq.d/fwrouter-ipv6-lan.conf`
- `/etc/dnsmasq.d/fwrouter-local-hosts.conf`
- `/etc/dnsmasq.d/fwrouter-upstream-dns.conf`
- `/etc/dnsmasq.d/lan.conf` — LAN DHCP keeps range/router option and static reservations, but DNS option `6` is intentionally commented out; FWRouter owns DNS advertisement through `/etc/dnsmasq.d/fwrouter-dhcp-dns.conf`.
- `/etc/dnsmasq.conf` — global example/manual `dhcp-option=option:dns-server,1.1.1.1,8.8.8.8` must stay commented out. LAN DHCP must advertise only router DNS (`192.168.0.1`); public secondary DNS breaks domain-aware selective routing and Android connectivity-check materialization. Current TP-Link reservations: Router1 `B0:A7:B9:89:93:F4 -> 192.168.0.9`, Router2 `5C:62:8B:02:9E:84 -> 192.168.0.10`.
- `/etc/dhcp/dhclient.conf`
- `/opt/fwrouter-mihomo/docker-compose.yml`
- `/opt/fwrouter-xray/docker-compose.yml`
- `/opt/fwrouter-api/.env`
- Nginx Proxy Manager state in `/app/NPM/data` and `npm-db-1` MariaDB; local LAN proxy hosts include `fwrouter.lan -> 192.168.0.1:5500` and `homes.lan -> 192.168.0.1:8123`.
- Public TLS for `vpn.minisk.ru` is owned by Nginx Proxy Manager cert `npm-27` (`/app/NPM/letsencrypt/live/npm-27`). Host `certbot.timer` is intentionally disabled: `/etc/letsencrypt/live/vpn.minisk.ru` is an obsolete expired host copy and must not be used as the renewal source while NPM owns ports `80/443`.

## Persistent state

- `/var/lib/fwrouter-v2/fwrouter.db`
- `/var/lib/fwrouter-v2/jobs/`
- `/var/lib/fwrouter-v2/cache/`
- `/var/lib/fwrouter-v2/state/`
- `/var/lib/fwrouter-v2/last-good/`

## Generated artifacts

- `/var/lib/fwrouter-v2/generated/dataplane/`
- `/var/lib/fwrouter-v2/generated/dataplane/profiles/`
- `/var/lib/fwrouter-v2/generated/mihomo/`
- `/var/lib/fwrouter-v2/generated/rules/`
- `/var/lib/fwrouter-v2/xray/config.json`

## Runtime-only state

- `/run/fwrouter-v2`
- live `nftables` table
- live `ip rules` and `ip routes`
- open ports and container processes

## Debug / archival artifacts

- `/var/lib/fwrouter-v2/debug/`
- `/var/lib/fwrouter-v2/backups/`

Эти каталоги полезны для расследований, но не являются source of truth для текущего desired state.

## Retention и write-churn

- runtime routing state и `nft/ip rule/ip route` не пишутся на диск на каждый пакет; это live kernel state
- успешный `repair_global_direct_runtime` обязан не только восстановить live direct contour, но и очистить stale `routing_global_state.apply_state/error_code/error_message`, иначе API показывает healthy runtime вместе со старой apply-ошибкой
- основной write-churn дают:
  - SQLite `/var/lib/fwrouter-v2/fwrouter.db`
  - technical/operational logs в `/var/log/fwrouter`
  - generated artifacts и dataplane snapshots
- precompiled global profiles тоже пишутся на диск, но это rebuildable optimization artifact, а не source of truth
- штатная очистка идет через:
  - `fwrouter-maintenance.timer`
  - `fwrouter-traffic-collect.timer`
- maintenance now also compacts oversized job results, reports storage by large artifact bucket, and prunes old `apply_versions` manifests/rows
- dataplane artifact retention is intentionally conservative: current/applied/last-good files and precompiled profiles are protected, while old versioned manifests, old last-good snapshots, and old job artifact dirs are eligible only through maintenance
- root-level `jobs/<job_id>/dataplane-manifest.json` is a compact summary/link for new applies; full dataplane manifests live in the canonical dataplane candidate/versioned paths
- если нужен разовый cleanup, использовать существующий `run_control_plane_maintenance()`, а не удалять файлы вручную
