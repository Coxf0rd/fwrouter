# `/etc/systemd/system/fwrouter_xray_service.md`

## Purpose

Starts the optional managed Xray Docker runtime.

## Review Notes

Read the source file directly before changing related behavior. Check adjacent service, route, adapter, script, or systemd documentation as applicable.

## Runtime Impact

Requires Docker, compose, TUN preflight via `FWROUTER_REQUIRE_TUN=1`, and the configured external Docker network. The network name defaults to `fwrouter_proxy` and can be overridden with `FWROUTER_DOCKER_PROXY_NETWORK`.

## Guardrails

- Keep FWRouter core as the authority for classification and policy routing.
- Keep Mihomo as a VPN egress adapter, not the network policy engine.
- Preserve direct-safe behavior for host/control-plane traffic unless an explicit scoped contour says otherwise.
