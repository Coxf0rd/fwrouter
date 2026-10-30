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

## Install and Update

Create the standard source checkout on a Debian/Ubuntu-like host. Keep it
root-controlled and clean; the local deploy command accepts a self-contained
standard checkout, not a linked worktree:

```bash
sudo apt-get update
sudo apt-get install -y git ca-certificates
sudo mkdir -p /srv
sudo git clone https://github.com/Coxf0rd/fwrouter.git /srv/fwrouter
cd /srv/fwrouter
```

Prepare the core control plane and UI with the normal installer:

```bash
sudo /srv/fwrouter/installer/install.sh --component backend --component host --component ui
```

The core works without bundled proxy runtimes. Install managed runtimes
separately only when this host is meant to run them:

```bash
sudo /srv/fwrouter/installer/install.sh --component mihomo
sudo /srv/fwrouter/installer/install.sh --component xray
```

For the repeatable local update workflow, install the deploy component from
this checkout and select it explicitly:

```bash
sudo /srv/fwrouter/installer/install.sh --deploy --component deploy
sudo /usr/local/sbin/fwrouter-deploy --configure-source /srv/fwrouter
sudo /usr/local/sbin/fwrouter-deploy --check
```

The checkout must be root-owned, clean (including ignored files), and have no
group/world-writable entries, links, linked worktree metadata, or alternate
Git object store. Selection pins its current full commit in root-owned local
configuration; it does not fetch source. Review the selected commit before
applying it with `sudo /usr/local/sbin/fwrouter-deploy`. Run `--check` again
afterward. When installed files and the full runtime health gate pass, it
reports `NOOP`; if it reports an unhealthy state, stop and resolve that
condition before trying another deployment path.

For an ordinary update, update the local Git checkout yourself, select the
reviewed revision, then refresh the installed deploy component if its source
bytes changed. The installed command checks that its module and wrapper match
the selected checkout, so refresh them before selecting that checkout:

```bash
sudo git -C /srv/fwrouter fetch origin
sudo git -C /srv/fwrouter switch --detach '<reviewed-commit>'
sudo /srv/fwrouter/installer/install.sh --deploy --component deploy
sudo /usr/local/sbin/fwrouter-deploy --configure-source /srv/fwrouter
sudo /usr/local/sbin/fwrouter-deploy --check
sudo /usr/local/sbin/fwrouter-deploy
sudo /usr/local/sbin/fwrouter-deploy --check
```

The deploy command applies backend/UI through the existing installer and
lifecycle owner, updates the existing host files when their bytes or modes
change, and preserves host-local data and runtime guards. Persistent timers
remain pending unless `--allow-timer-catchup` is explicitly requested. A
conflicting pending operation blocks a new update; keep its checkpoint intact
and follow the documented lifecycle procedure. This public updater is not a
require a private orchestrator, automatically create backups or JIT snapshots,
or perform automatic rollback.

The updater's health gate requires the API, Gateway listener, and active
Mihomo and Xray units. Installing only the bare core does not satisfy that
runtime gate; install and enable the managed runtimes when using this updater.
For first-time setup or installations that do not meet this gate, use the
normal installer commands described in
[`installer/README.md`](installer/README.md) and
[`knowledge/INSTALL_AND_DEPLOY.md`](knowledge/INSTALL_AND_DEPLOY.md).

## Development And Deployment Flow

Develop in this git repository:

```bash
cd /srv/fwrouter
```

Use `fwrouter-deploy` above for ordinary updates from this local checkout.
The source installer remains available for initial setup and explicit
component-focused cases. For example, deploy all components on a prepared
host with:

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
