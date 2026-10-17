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
- `/opt/fwrouter-api/.env` is root-only (`0600`) and holds ordinary deployment configuration. Credentials must use dedicated secret inputs/persistent control-plane intent and must not be placed in user display settings.

LAN DHCP must advertise only router DNS. Public secondary DNS breaks domain-aware selective routing because clients can bypass router-owned DNS materialization.

Deployment-specific network bindings are configured in one backend env block in `/opt/fwrouter-api/.env`: protected CIDRs, rules-only extra protected CIDRs, trusted client CIDRs, LAN interface allow/deny filters, and local LAN hostnames. Values are JSON arrays/objects under `FWROUTER_*`; `services/network_contract.py` normalizes them and writes the bounded contract into dataplane manifests for renderer and host scripts. Any valid deployment CIDR/interface name can be supplied. Nginx Proxy Manager upstream targets such as `fwrouter.lan -> <router-ip>:5500` remain NPM-owned and must be changed there when the host LAN address changes.

Nginx Proxy Manager owns local LAN proxy hosts and public TLS for the deployment-specific FWRouter public ingress. Keep the deployment hostname in live env/config, not in source. Host-level `certbot.timer` is intentionally disabled while NPM owns ports `80/443` and certificate renewal.

## Persistent State

- `/var/lib/fwrouter-v2/fwrouter.db`
- State root is private (`0700`); SQLite database and WAL/SHM sidecars are private (`0600`). Backup directory/files use `0700`/`0600`.
- State root is private (`0700`); SQLite database and WAL/SHM sidecars are private (`0600`). Backup directory/files use `0700`/`0600`.
- `/var/lib/fwrouter-v2/jobs/`
- `/var/lib/fwrouter-v2/cache/`
- `/var/lib/fwrouter-v2/state/`
- `/var/lib/fwrouter-v2/last-good/`

SQLite stores persistent intent, operational metadata, jobs, logs, and accounting state that must survive reboot.

Subscription inventory acquisition and native publication are separate durability boundaries. The internal generation admission marker in `settings` tracks pending versus native-verified inventory; it is not a replacement for canonical rows, generation checkpoints or native proof. See the [pending inventory recovery change](../audits/pending_inventory_verified_generation_fix_2026-10-16/REPORT.md) for admission, crash/restart and legacy-upgrade constraints. Source acceptance does not authorize production activation. Before another inventory refresh, the existing generation owner must recover/prove any open checkpoint. Ordinary inventory writers recheck under the shared writer guard and refuse every unresolved checkpoint; provider network work remains outside that guard. Disabled/external Xray ownership never authorizes native recovery. Explicit reviewed identity-migration supersession remains a separate exact checkpoint/plan/ledger operation.

## Explicit Legacy Inventory Admission

A nonempty pre-marker inventory remains unverified after upgrade; generic startup must not grant it admission. The explicit proof-only owner operation is distinct from provider refresh and native apply. It may persist an operation checkpoint, fresh proof receipt and generation marker, but must not prepare credentials, fetch providers, change routing or apply/reload/restart native components. Incomplete proof, drift or stale ownership/revision/incarnation fences blocks publication.

Proof-only checkpoints have their own operation mode. Ordinary recovery must refuse them before any restore/reload path; only explicit operator re-entry can re-prove the bound inventory and complete or close that operation. A verified marker never replaces fresh native proof, last-good protection or a reviewed identity-migration/supersession contract. See the [implementation contract and qualification status](../audits/proof_only_legacy_admission_2026-10-17/REPORT.md). Source availability does not authorize production execution.

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
