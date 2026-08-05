# FWRouter

FWRouter is a Linux traffic-routing control plane. Its core manages global and selective traffic routing, persistent intent/state, `nftables`, policy routing, and a local web UI without requiring bundled proxy runtimes. Mihomo, Xray, Tailscale, Docker, and other ingress/egress services are optional integrations layered onto the routing core.

This repository is the source-of-truth tree. Live server paths such as `/opt/fwrouter-*`, `/etc/systemd/system`, `/usr/local/libexec/fwrouter`, and `/var/lib/fwrouter-v2` are deployment targets, not git working trees.

## Components

- [`backend/`](backend/README.md) - FastAPI control-plane, SQLite intent/state model, apply/reconcile jobs, API routes and tests.
- [`ui/`](ui/README.md) - static operator/user web interface served by the backend or reverse proxy.
- [`runtimes/mihomo/`](runtimes/mihomo/README.md) - Mihomo Docker runtime wrapper for transparent egress.
- [`runtimes/xray/`](runtimes/xray/README.md) - Xray Docker runtime wrapper for subscription clients.
- [`host/`](host/README.md) - systemd units, privileged dataplane scripts, sysctl and policy-routing fragments.
- [`installer/`](installer/README.md) - source-tree installer, dependency bootstrap and surface checks.
- [`knowledge/`](knowledge/README.md) - persistent architecture and operations knowledge map.

## Optional Integrations

FWRouter supports two integration styles:

- managed integration: FWRouter installs and manages a bundled runtime. The current bundled managed runtimes are the Mihomo and Xray Docker wrappers.
- external integration: any existing service is connected manually while FWRouter keeps ownership of classification, policy routing, and control-plane state. Tailscale is the current built-in external integration example, but `external` is not Tailscale-specific.

Developer-facing external connection contracts are documented in [`knowledge/EXTERNAL_CONNECTIONS.md`](knowledge/EXTERNAL_CONNECTIONS.md).

The core `backend` + `host` install is expected to work without installing or enabling Mihomo, Xray, Docker, or Tailscale.

## Install

Create or update the source tree from Git on a Debian/Ubuntu-like host:

```bash
sudo apt-get update
sudo apt-get install -y git ca-certificates
sudo mkdir -p /srv
sudo git clone https://github.com/Coxf0rd/fwrouter.git /srv/fwrouter
cd /srv/fwrouter
```

If `/srv/fwrouter` already exists, update it instead:

```bash
cd /srv/fwrouter
sudo git pull --ff-only
```

Install the core control plane without bundled proxy runtimes:

```bash
sudo /srv/fwrouter/installer/install.sh --component backend --component host --component ui
```

Install all bundled components on a Debian/Ubuntu-like host:

```bash
sudo /srv/fwrouter/installer/install.sh --all
```

Or install selected components:

```bash
sudo /srv/fwrouter/installer/install.sh --component backend
sudo /srv/fwrouter/installer/install.sh --component ui
sudo /srv/fwrouter/installer/install.sh --component mihomo
sudo /srv/fwrouter/installer/install.sh --component xray
sudo /srv/fwrouter/installer/install.sh --component host
```

The installer copies source components into their live paths, prepares host dependencies when installing to `/`, bootstraps state directories, and enables FWRouter systemd units/timers for host installs.

## Development And Deployment Flow

Develop in this git repository:

```bash
cd /srv/fwrouter
```

Deploy changes into the live server paths with the installer:

```bash
sudo /srv/fwrouter/installer/install.sh --all
```

For focused changes, deploy only the affected component:

```bash
sudo /srv/fwrouter/installer/install.sh --component backend
sudo systemctl restart fwrouter-api.service
```

This is not a static archive-only export. `/srv/fwrouter` is the editable source tree; `/opt/fwrouter-*`, `/etc/systemd/system`, `/usr/local/libexec/fwrouter`, and `/usr/local/sbin` are the installed runtime targets. Git tracks the source tree, then the installer copies the selected components into place.

## Important Boundaries

- Secrets stay out of git. Use `backend/.env.example` as the template for `/opt/fwrouter-api/.env`.
- Runtime state stays out of git: `/var/lib/fwrouter-v2`, `/var/log/fwrouter`, `/run/fwrouter-v2`.
- Generated configs are rebuildable and should not be committed unless they are explicit source templates.
- The live deployment can be regenerated from this repository plus host-local secrets/state.
