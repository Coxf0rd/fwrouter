# FWRouter

FWRouter is a Linux router control-plane and dataplane project. It manages global and selective traffic routing through `nftables`, policy routing, Mihomo, Xray subscriptions, and a local web UI.

This repository is the source-of-truth tree. Live server paths such as `/opt/fwrouter-*`, `/etc/systemd/system`, `/usr/local/libexec/fwrouter`, and `/var/lib/fwrouter-v2` are deployment targets, not git working trees.

## Components

- [`backend/`](backend/README.md) - FastAPI control-plane, SQLite intent/state model, apply/reconcile jobs, API routes and tests.
- [`ui/`](ui/README.md) - static operator/user web interface served by the backend or reverse proxy.
- [`runtimes/mihomo/`](runtimes/mihomo/README.md) - Mihomo Docker runtime wrapper for transparent egress.
- [`runtimes/xray/`](runtimes/xray/README.md) - Xray Docker runtime wrapper for subscription clients.
- [`host/`](host/README.md) - systemd units, privileged dataplane scripts, sysctl and policy-routing fragments.
- [`installer/`](installer/README.md) - source-tree installer, dependency bootstrap and surface checks.
- [`решения/`](решения/README.md) - persistent architecture and operations knowledge map.
- [`docs/`](docs/) - secondary notes and fix logs.

## Install

Install all components on a Debian/Ubuntu-like host:

```bash
sudo /srv/fwrouter/installer/install.sh --all
```

Install selected components:

```bash
sudo /srv/fwrouter/installer/install.sh --component backend
sudo /srv/fwrouter/installer/install.sh --component ui
sudo /srv/fwrouter/installer/install.sh --component mihomo
sudo /srv/fwrouter/installer/install.sh --component xray
sudo /srv/fwrouter/installer/install.sh --component host
```

The installer copies source components into their live paths, prepares host dependencies when installing to `/`, bootstraps state directories, and enables FWRouter systemd units/timers for host installs.

## Important Boundaries

- Secrets stay out of git. Use `backend/.env.example` as the template for `/opt/fwrouter-api/.env`.
- Runtime state stays out of git: `/var/lib/fwrouter-v2`, `/var/log/fwrouter`, `/run/fwrouter-v2`.
- Generated configs are rebuildable and should not be committed unless they are explicit source templates.
- The live deployment can be regenerated from this repository plus host-local secrets/state.

