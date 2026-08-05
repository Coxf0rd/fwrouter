# Boot Flow

## Services That Must Be Enabled

- `fwrouter-mihomo.service`
- `fwrouter-xray.service`
- `fwrouter-api.service`
- `fwrouter-xray-sub-gateway.service`
- `fwrouter-subscription-refresh.timer`
- `fwrouter-maintenance.timer`
- `fwrouter-jobs-retention-dry-run.timer`
- `fwrouter-traffic-collect.timer`

## Startup Order

1. `network-online.target`
2. `fwrouter-api.service`
3. Optional managed runtimes such as `fwrouter-mihomo.service` and `fwrouter-xray.service`
4. Optional follower services such as `fwrouter-xray-sub-gateway.service`
6. timers and regular jobs

Race-condition protections:

- `fwrouter-api.service` has `After/Wants=network-online.target` only, so the core control plane can start without optional runtimes
- Mihomo startup waits for `127.0.0.1:5200`
- the subscription gateway waits for `127.0.0.1:5000`
- Xray startup requires `docker network inspect "$FWROUTER_DOCKER_PROXY_NETWORK"`; default `fwrouter_proxy`

## Required Before Backend Startup

- `/dev/net/tun`
- `nft` and `ip` commands
- Docker daemon
- persistent and runtime directories
- `sysctl` with `src_valid_mark=1`, `ip_forward=1`, `rp_filter=0`
- routing table alias `100 fwrouter_vpn`

## What Backend Must Create

- `/var/lib/fwrouter-v2/*` bootstrap directories
- `/var/log/fwrouter/*`
- `/run/fwrouter-v2`
- live owned `nftables` table and `ip rule/ip route` when missing after reboot
- Mihomo selector restore and intended routing recovery
- scoped LAN/Tailscale subject rules when SQLite intent says `direct/selective/vpn` but live `fwrouter_classify` lacks subject-specific rules

## What Does Not Survive Reboot

- `nftables` table `inet fwrouter_v2`, unless a separate system firewall unit recreates it
- `ip rule`
- `ip route` in the custom table
- live listener/process state of containers
- correspondence between live `fwrouter_classify` and persisted per-client overrides after manual/emergency dataplane reload

## What Must Be Persistent

- `/etc/sysctl.d/99-fwrouter-routing.conf`
- `/etc/iproute2/rt_tables.d/fwrouter.conf`
- systemd units and timers
- generated configs in `/var/lib/fwrouter-v2/generated/`
- last-good snapshots and SQLite state

## Directories

Created ahead of time or by bootstrap:

- `/var/lib/fwrouter-v2`
- `/var/lib/fwrouter-v2/generated`
- `/var/lib/fwrouter-v2/jobs`
- `/var/lib/fwrouter-v2/cache`
- `/var/lib/fwrouter-v2/state`
- `/var/log/fwrouter`

Runtime-only:

- `/run/fwrouter-v2`
- debug dumps inside `/var/lib/fwrouter-v2/debug`

## Reboot Verification

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

## If Scoped Client Rules Disappear After Backend Restart

Scenario: SQLite/API show client `desired_mode=selective` or `vpn`, but the live chain contains only `goto fwrouter_direct comment "global direct v1"`.

Expected protection:

- `bootstrap.recover_startup_scoped_subject_routing()` reads active LAN/Tailscale subjects from SQLite
- it then reads `nft list chain inet fwrouter_v2 fwrouter_classify`
- if a persisted scoped subject is absent from the live chain, backend runs normal `set_subject_admin_mode(..., requested_by="startup-scoped-subject-recovery")`
- re-apply must rebuild a subject-aware manifest; `global=direct` must not be considered pure-direct runtime while per-client `selective`/`vpn` exists
