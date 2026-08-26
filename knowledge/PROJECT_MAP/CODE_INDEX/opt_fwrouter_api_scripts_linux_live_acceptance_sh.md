# `/opt/fwrouter-api/scripts_linux_live_acceptance.sh`

## Purpose

Live acceptance script for main API/runtime flows on the server.

## Review Notes

Read the source file directly before changing related behavior. It performs real API mutations.

## Runtime Impact

Runtime impact is not read-only: it may change global routing, core bypass state, and optional client subject server overrides. It must not manage external provider runtime lifecycle.

## Guardrails

- Keep FWRouter core as the authority for classification and policy routing.
- Keep Mihomo as a VPN egress adapter, not the network policy engine.
- Preserve direct-safe behavior for host/control-plane traffic unless an explicit scoped contour says otherwise.
