# `/opt/fwrouter-api/fwrouter_api_adapters_scripts.py`

## Purpose

Defines the allowlisted host commands that backend services may execute without
accepting arbitrary shell input. The allowlist includes dataplane scripts,
Tailscale/Docker/host inventory helpers, traffic collection and selected
systemd actions.

## Review Notes

Read the source file directly before changing related behavior. Check adjacent service, route, adapter, script, or systemd documentation as applicable.

## Runtime Impact

`docker_inventory` runs `/usr/local/libexec/fwrouter/docker-inventory.py` and is
the preferred Docker subject source. `docker_ps` remains as a legacy fallback so
partial deploys do not break Docker discovery.

## Guardrails

- Keep FWRouter core as the authority for classification and policy routing.
- Keep Mihomo as a VPN egress adapter, not the network policy engine.
- Preserve direct-safe behavior for host/control-plane traffic unless an explicit scoped contour says otherwise.
