# fwrouter-stack (public export)

This repo is a **sanitized export** of a home gateway stack:

- `fwrouter/`: local UI/API (FastAPI) + docker-compose
- `fwrouter/docker-compose.mihomo2.yml`: Mihomo2 (Clash Meta) in `network_mode: host` with TUN
- `host-sbin/`: scripts expected under `/usr/local/sbin` (apply rules, health-check, etc.)
- `host-systemd/`: systemd units under `/etc/systemd/system` (paths/timers/services)
- `host-etc-fwrouter/`: **examples** for `/etc/fwrouter` (no real secrets/domains)

## Public / privacy notes

This export intentionally removes or replaces:

- subscription URLs, HWID headers, REALITY private keys, TLS certs/keys
- generated subscriptions (`sub-vpn*`), generated Xray configs, device name caches
- any personal hostnames/domains from the UI where possible

You must provide secrets locally on the target host (see `.env.example` files and config examples).

## Quick layout (runtime)

- Configs: `/etc/fwrouter/*`
- State: `/var/lib/fwrouter/*`
- Binaries/scripts: `/usr/local/sbin/fwrouter-*`
- Units: `/etc/systemd/system/fwrouter-*`
- dnsmasq drop-ins: `/etc/systemd/system/dnsmasq.service.d/*`

## Next step

Use Ansible installer skeleton in `ansible/` (optional), or install manually by copying
`host-sbin/` + `host-systemd/` + `host-etc-fwrouter/` to their target paths.
