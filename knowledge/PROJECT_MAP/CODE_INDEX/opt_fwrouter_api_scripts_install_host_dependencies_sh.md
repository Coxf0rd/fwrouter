# `/opt/fwrouter-api/scripts_install_host_dependencies.sh`

## Purpose

Installs FWRouter host dependencies on Debian/Ubuntu-like hosts through `apt-get`.

## Behavior Notes

- Supports `--dry-run` to print the selected package list without installing.
- Installs base tools for nftables, policy routing, Python backend setup, DNS, archives, and conntrack.
- Skips Debian `docker.io` when a `docker` command already exists, so Docker CE/containerd.io hosts do not hit apt package conflicts.
- Skips Docker compose packages when `docker compose` already works.

## Review Notes

Read the source file directly before changing related behavior. Check adjacent service, route, adapter, script, or systemd documentation as applicable.

## Runtime Impact

Installs apt packages by selected component. `backend` receives the minimal Python/SQLite toolchain, `host` receives Linux dataplane tools, `mihomo`/`xray` receive Docker/compose candidates and TUN tooling, and `ui` has no system runtime packages. `--dry-run` prints the package set without apt mutation.

## Guardrails

- Keep FWRouter core as the authority for classification and policy routing.
- Keep Mihomo as a VPN egress adapter, not the network policy engine.
- Preserve direct-safe behavior for host/control-plane traffic unless an explicit scoped contour says otherwise.
