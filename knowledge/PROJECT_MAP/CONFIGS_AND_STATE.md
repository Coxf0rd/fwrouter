# Configs And State

## Persistent Config

Persistent host config includes:

- `/etc/systemd/system/fwrouter-*.service`
- `/etc/systemd/system/fwrouter-*.timer`
- `/etc/sysctl.d/99-fwrouter-routing.conf`
- `/etc/iproute2/rt_tables.d/fwrouter.conf`
- `/etc/dnsmasq.d/fwrouter-rules.conf`
- `/etc/dnsmasq.d/fwrouter-dhcp-dns.conf`
- `/etc/dnsmasq.d/fwrouter-ipv6-lan.conf`
- `/etc/dnsmasq.d/fwrouter-local-hosts.conf`
- `/etc/dnsmasq.d/fwrouter-upstream-dns.conf`
- `/etc/dnsmasq.d/lan.conf`
- `/etc/dnsmasq.conf`
- `/etc/dhcp/dhclient.conf`
- `/opt/fwrouter-mihomo/docker-compose.yml`
- `/opt/fwrouter-xray/docker-compose.yml`
- `/opt/fwrouter-api/.env`

LAN DHCP must advertise only router DNS. Public secondary DNS breaks domain-aware selective routing because clients can bypass router-owned DNS materialization.

Nginx Proxy Manager owns local LAN proxy hosts and public TLS for `vpn.minisk.ru`. Host-level `certbot.timer` is intentionally disabled while NPM owns ports `80/443` and certificate renewal.

## Persistent State

- `/var/lib/fwrouter-v2/fwrouter.db`
- `/var/lib/fwrouter-v2/jobs/`
- `/var/lib/fwrouter-v2/cache/`
- `/var/lib/fwrouter-v2/state/`
- `/var/lib/fwrouter-v2/last-good/`

SQLite stores persistent intent, operational metadata, jobs, logs, and accounting state that must survive reboot.

## Generated Artifacts

- `/var/lib/fwrouter-v2/generated/dataplane/`
- `/var/lib/fwrouter-v2/generated/dataplane/profiles/`
- `/var/lib/fwrouter-v2/generated/mihomo/`
- `/var/lib/fwrouter-v2/generated/rules/`
- `/var/lib/fwrouter-v2/xray/config.json`

Generated artifacts are rebuildable or promoted state, not hand-edited source.

## Runtime-Only State

- `/run/fwrouter-v2`
- live nftables table
- live `ip rule` and `ip route` state
- open ports and container processes

Live kernel state is never the source of truth. Backend startup recovery must recreate it from persistent intent.

## Debug And Retention

- `/var/lib/fwrouter-v2/debug/`
- `/var/lib/fwrouter-v2/backups/`

Debug and backup directories are useful for investigation but are not desired-state sources.

Main write-churn sources are SQLite, operational/technical logs, generated artifacts, dataplane snapshots, and precompiled global profiles. Maintenance timers prune conservatively; current/applied/last-good artifacts and valid precompiled profiles must remain protected.

Use existing maintenance functions for cleanup instead of deleting runtime files manually.
