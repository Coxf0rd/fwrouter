# FWRouter Architecture

`fwrouter` is the control-plane and dataplane wrapper for Linux host network routing. Its core owns global network mode, selective/VPN rules, SQLite intent/state, and kernel state through `nftables`, `ip rule`, `ip route`, and `sysctl`. `mihomo`, `xray`, Docker, and external ingress/egress runtimes are optional integrations layered onto the routing core.

## Main Components

- `fwrouter-api` in `/opt/fwrouter-api`
  Purpose: FastAPI backend, SQLite intent/state storage, apply/reconcile job orchestration, runtime/API diagnostics.
- `mihomo` runtime
  Purpose: optional managed egress dataplane adapter, controller on `127.0.0.1:5200`, mixed listener, transparent TProxy/listener contour.
- `xray` runtime
  Purpose: optional managed proxy runtime and client subscriptions; not the owner of host policy routing.
- external integrations
  Purpose: user-managed network services that bring client-plane traffic, egress endpoints, or identity into FWRouter. Concrete connections are persisted in the `external_connections` registry and keyed by stable `connection_id`. A connection declares its role (`external_management`, `external_vpn_module`, `external_network_source`, `display_only`) and data delivery mode (`api_push`, `http_poll`, `command_probe`, `file_read`). External ingress providers are registry contracts/templates: transport remains outside FWRouter lifecycle control, while concrete connection instances are created through UI/API or one-time upgrade migration. Generated state, collector runtime state, probe cache, and imported external-ingress subjects must stay scoped to the concrete `connection_id`.
- `systemd` units
  Purpose: boot ordering, persistence, timers, preflight, restart behavior.
- `dnsmasq` host service
  Purpose: LAN DNS/DHCP and DNS-answer materialization into FWRouter-owned
  nft timeout sets for domain-aware selective routing. FWRouter owns a minimal
  systemd drop-in so dnsmasq restarts after runtime nftset failures.
- libexec scripts in `/usr/local/libexec/fwrouter`
  Purpose: apply/check dataplane, wait for readiness, collect traffic, run the Xray gateway, and watch Docker inventory events.

## Persistent Config

- `/etc/systemd/system/fwrouter-*.service`
- `/etc/systemd/system/fwrouter-*.timer`
- `/etc/sysctl.d/99-fwrouter-routing.conf`
- `/etc/iproute2/rt_tables.d/fwrouter.conf`
- `/opt/fwrouter-mihomo/docker-compose.yml`
- `/opt/fwrouter-xray/docker-compose.yml`
- `/opt/fwrouter-api/.env`
- SQLite state `/var/lib/fwrouter-v2/fwrouter.db`

## Generated Configs

- `/var/lib/fwrouter-v2/generated/dataplane/*.json`
- `/var/lib/fwrouter-v2/generated/dataplane/applied-manifest.json`
- `/var/lib/fwrouter-v2/generated/dataplane/profiles/{direct,selective,vpn}.json`
- `/var/lib/fwrouter-v2/generated/mihomo/config.yaml`
- `/var/lib/fwrouter-v2/generated/mihomo/config.next.yaml`
- `/var/lib/fwrouter-v2/generated/mihomo/contours.json`
- `/var/lib/fwrouter-v2/xray/config.json`

## Runtime State

- live `nftables` table `inet fwrouter_v2`
- live `ip rule` entries for fwmarks
- live `ip route` entry in table `100`
- runtime dirs `/run/fwrouter-v2` and `/var/lib/fwrouter-v2/state`
- Docker containers `fwrouter-mihomo` and `fwrouter-xray`

## Component Relationships

- `fwrouter-api.service` starts after `network-online.target`, runs core `ExecStartPre` preflight, then backend startup.
- Managed runtime units such as `fwrouter-mihomo.service` and `fwrouter-xray.service` are enabled only when their components are installed.
- Runtime integrations are tracked in `modules.lifecycle_mode`: `managed` means FWRouter owns the lifecycle and may write runtime configs or restart units/containers, `external` means FWRouter may probe/use an already existing service but must not manage its lifecycle, and `none` means the integration is absent. Mihomo/Xray are the bundled managed runtime paths; external ingress providers remain external-only with no FWRouter-managed `start/stop/restart`. The UI is an install component, not a runtime module.
- Clean database bootstrap creates only core module rows. Optional provider/runtime rows such as `xray` and `tailscale` are created on explicit user/API action or preserved during migration when they carry real user state.
- Backend startup through `bootstrap_backend()` restores directories, database, builtin subjects, `dnsmasq`, Mihomo selector state, and live dataplane after reboot when needed.
- Backend startup starts the subject inventory scheduler; it periodically creates `subject_inventory_sync` jobs for Docker/Host so the UI does not depend on manual sync.
- Backend startup starts the external collector scheduler, but it does not poll `api_push` or manual connections; collectors run only for enabled external connections with `refresh_mode=interval`.
- `external_vpn_module` may own one active dataplane/explicit-client replacement per `replacement_target`; `external_network_source` and `external_management` allow multiple instances of the same provider.
- The runtime apply pipeline writes generated artifacts, generates Mihomo config, and calls libexec scripts for `nftables` and policy routing.
- Background prewarm after startup/apply builds short-lived in-memory caches and precompiled global dataplane profiles for fast global mode activation.
- `fwrouter-xray-sub-gateway.service` exposes a separate HTTP endpoint on `172.18.0.1:5055` and proxies subscriptions into the API.
- `fwrouter-docker-subject-events.service` listens to `docker events` and triggers fast Docker-only inventory sync through the local API; the periodic backend scheduler remains the fallback.

## Privileges

- root or equivalent privileges are required for `nft`, `ip`, `sysctl`, and systemd unit installation. Managed Mihomo/Xray runtime installs additionally require Docker Compose and `/dev/net/tun`.
- the `mihomo` container needs `NET_ADMIN`, `NET_RAW`, and `/dev/net/tun`.
- the backend must not store ephemeral runtime state in persistent configs as source of truth.

## Main Failure Points

- `network-online.target` does not guarantee that required interfaces, Docker, and local ports are ready.
- missing `/dev/net/tun` for managed runtime units
- missing configured Docker network for `xray`; default is `fwrouter_proxy`, with `FWROUTER_DOCKER_PROXY_NETWORK=proxy_net` available for legacy deployments
- `dnsmasq` nftset materialization failures under high DNS churn
- drift between SQLite intent and live kernel dataplane after reboot or partial failure
- invalid generated `mihomo` config or unavailable controller `127.0.0.1:5200`
- duplicate `ip rule` entries if current idempotent apply/rollback logic is broken
