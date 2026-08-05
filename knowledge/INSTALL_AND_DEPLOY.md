# Install And Deploy

## Source Tree

The canonical git/source root is `/srv/fwrouter`.

Component layout:

- `backend/` -> `/opt/fwrouter-api`
- `ui/` -> `/opt/fwrouter-ui`
- `runtimes/mihomo/` -> `/opt/fwrouter-mihomo`
- `runtimes/xray/` -> `/opt/fwrouter-xray`
- `host/systemd/` -> `/etc/systemd/system`
- `host/libexec/fwrouter/` -> `/usr/local/libexec/fwrouter`
- `host/sbin/` -> `/usr/local/sbin`
- `host/sysctl.d/` -> `/etc/sysctl.d`
- `host/iproute2/` -> `/etc/iproute2`
- `installer/` -> source-level install and validation tooling

`/opt`, `/etc`, `/usr/local`, `/var/lib`, `/var/log`, and `/run` are deployment/runtime targets, not the primary git working tree.

## Fresh Git Install

On a new Debian/Ubuntu-like host, first create the source tree from Git:

```bash
sudo apt-get update
sudo apt-get install -y git ca-certificates
sudo mkdir -p /srv
sudo git clone https://github.com/Coxf0rd/fwrouter.git /srv/fwrouter
cd /srv/fwrouter
```

If the repository is already present, update the source tree before deploying:

```bash
cd /srv/fwrouter
sudo git pull --ff-only
```

Install the core control plane plus UI, without bundled Mihomo/Xray runtimes:

```bash
sudo /srv/fwrouter/installer/install.sh --component backend --component host --component ui
```

Install managed runtime wrappers only when this host should run FWRouter-managed Mihomo or Xray:

```bash
sudo /srv/fwrouter/installer/install.sh --component mihomo
sudo /srv/fwrouter/installer/install.sh --component xray
```

Host-local secrets and runtime state are not cloned from Git. Configure `/opt/fwrouter-api/.env` from `backend/.env.example` after installing if local settings are needed.

## Main Installer

Use `/srv/fwrouter/installer/install.sh`.

The installer can deploy all components or one focused component:

```bash
/srv/fwrouter/installer/install.sh --all
/srv/fwrouter/installer/install.sh --component backend
/srv/fwrouter/installer/install.sh --component ui
/srv/fwrouter/installer/install.sh --component mihomo
/srv/fwrouter/installer/install.sh --component xray
/srv/fwrouter/installer/install.sh --component host
```

At target `/`, the installer may also install component-scoped dependencies, prepare the backend venv, install systemd units/timers, install sysctl and policy-routing fragments, run backend bootstrap state setup, run `systemctl daemon-reload`, enable selected services/timers, and apply `sysctl --system`. Docker network creation is limited to selected managed runtime components (`mihomo` or `xray`) and uses `FWROUTER_DOCKER_PROXY_NETWORK`, defaulting to `fwrouter_proxy`.

The installer must not copy `.env`, `.venv`, SQLite databases, generated runtime state, logs, caches, backup files, archives, `__pycache__`, `.pytest_cache`, or `*.pyc`.

## Source Contract

- Git stores the component source tree, not a live server dump.
- Secrets and runtime state remain host-local.
- Run `/srv/fwrouter/installer/check-clean-tree-surface.sh` before commit or deploy.
- Old live helpers under `/opt/fwrouter-api/scripts/` may still be present on the host, but source-level installation is owned by `/srv/fwrouter/installer/`.

## Bootstrap State

`/opt/fwrouter-api/scripts/bootstrap-state.sh` creates runtime directories:

- `/var/lib/fwrouter-v2/{cache,generated,jobs,state,last-good,rules,xray}`
- `/var/log/fwrouter/{operational,technical,xray}`
- `/run/fwrouter-v2`

## Manual Deployment Minimum

Core-only host:

```bash
/srv/fwrouter/installer/install-host-dependencies.sh --yes --component backend --component host
/srv/fwrouter/installer/install.sh --component backend --component host
systemctl daemon-reload
systemctl enable fwrouter-api.service fwrouter-maintenance.timer fwrouter-jobs-retention-dry-run.timer fwrouter-traffic-collect.timer
systemctl restart fwrouter-api.service
```

Bundled managed Mihomo/Xray runtimes are installed separately:

```bash
/srv/fwrouter/installer/install.sh --component mihomo
/srv/fwrouter/installer/install.sh --component xray
systemctl daemon-reload
systemctl enable fwrouter-mihomo.service fwrouter-xray.service fwrouter-xray-sub-gateway.service fwrouter-subscription-refresh.timer
systemctl restart fwrouter-mihomo.service fwrouter-xray.service fwrouter-xray-sub-gateway.service
```

`install-host-dependencies.sh` targets Debian/Ubuntu-like hosts with `apt-get` and accepts `--component` plus `--dry-run`. Component package scopes are:

- `backend`: Python/venv/SQLite and minimal transfer/archive tools.
- `host`: `nftables`, `iproute2`, `iptables`, `conntrack`, `dnsmasq`, `dnsutils`, `procps`, and `kmod`.
- `mihomo`/`xray`: Docker/compose candidates when missing plus TUN support tooling.
- `ui`: no system runtime packages.

If `docker` and `docker compose` already exist, Docker packages are skipped so Docker CE/containerd.io hosts do not conflict with Debian `docker.io`. Non-apt distributions need a separate package mapping.

`conntrack` is optional for apply success. When present, apply can clear old client IPv4 flows after VPN/selective dataplane changes so new TCP connections pass through fresh transparent redirect/TProxy rules.

## Safe Deployment Sequence

1. Edit source in `/srv/fwrouter`.
2. Run focused tests or validation.
3. Run `/srv/fwrouter/installer/check-clean-tree-surface.sh`.
4. Commit source changes.
5. Deploy the affected component with `installer/install.sh`.
6. Restart or reload only the affected services.
7. Smoke-check runtime with boot/dataplane diagnostics.
