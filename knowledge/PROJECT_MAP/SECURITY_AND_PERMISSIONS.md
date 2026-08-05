# Security And Permissions

## Required Privileges

FWRouter needs root-level privileges for host networking operations:

- `nft`
- `ip rule`
- `ip route`
- `sysctl`
- `iptables` DNS capture rules
- systemd unit installation/reload
- Docker Compose runtime management

The Mihomo container requires:

- `NET_ADMIN`
- `NET_RAW`
- `/dev/net/tun`

## Secrets And Local State

The git source tree must not contain:

- `.env`
- `.venv`
- SQLite databases
- generated runtime artifacts
- logs
- backups
- subscription secrets
- private keys or certificates

These are host-local deployment/runtime concerns.

## Control-Plane Safety

- FWRouter own traffic stays direct-safe by default.
- Protected management/control-plane destinations must not be captured into VPN loops.
- External management clients must provide complete attribution before mutating state.
- Bypass mode requires explicit confirmation and is applied through a job.
