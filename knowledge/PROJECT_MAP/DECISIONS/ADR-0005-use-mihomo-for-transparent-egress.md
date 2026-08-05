# 0005: Use Mihomo For Transparent Egress

## Status

Accepted.

## Context

FWRouter needs a runtime that supports transparent/TProxy egress, selectors, health probes, and a controller API.

## Decision

Use Mihomo as the VPN egress adapter with controller `127.0.0.1:5200`, selector `vpn-global`, and FWRouter-owned transparent listeners.

## Consequences

- Mihomo provides transparent egress and selector mechanics.
- FWRouter remains the authority for classification and policy routing.
- Generated config and controller readiness become critical.
- Missing `/dev/net/tun` or selector drift can break intended routing.

## Related Files

- `/opt/fwrouter-mihomo/docker-compose.yml`
- `/opt/fwrouter-api/fwrouter_api/services/mihomo_config.py`
- `/opt/fwrouter-api/fwrouter_api/adapters/mihomo.py`
- `/etc/systemd/system/fwrouter-mihomo.service`
