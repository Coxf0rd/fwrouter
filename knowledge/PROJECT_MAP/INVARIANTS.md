# Invariants

- `/srv/fwrouter` is the git/source root. Live paths under `/opt`, `/etc`, `/usr/local`, `/var/lib`, `/var/log`, and `/run` are deployment/runtime targets.
- FWRouter core is the routing authority. Mihomo is an egress adapter, not the network policy engine.
- `fwrouter:global` represents FWRouter own traffic and must stay direct-safe. It must not become a normal user-facing VPN subject.
- Client-plane subjects are `lan`, `external_network_client`, and `explicit_external_client`; provider-specific legacy subject names are normalized at service/migration boundaries.
- System/control subjects are `host`, `docker`, and `fwrouter`.
- Xray clients remain forced VPN through their explicit ingress path.
- Host and Docker traffic are direct by default; explicit scoped VPN is valid only when a stable matcher exists.
- Domain-aware selective routing requires router-owned DNS materialization through `dnsmasq` and nft sets.
- Live kernel dataplane state is rebuildable runtime state. SQLite intent and generated/last-good artifacts are the durable source of truth.
- Apply/rollback must remain idempotent and must not leave duplicate `ip rule` entries.
- Startup recovery must recreate missing live dataplane state after reboot without rewriting intended routing to direct.
- Unit tests must not touch live host dataplane, live probes, systemd, Docker runtime, or the production SQLite state unless the test is explicitly marked as live acceptance.
- Project documentation in git must be English. Local non-English notes belong outside the repo in the owner-local decisions tree.
