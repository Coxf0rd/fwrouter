# `/opt/fwrouter-api/fwrouter_api_services_tailscale.py`

## Purpose

Read-only host-probe for Tailscale through allowlisted `tailscale status --json`.

## Review Notes

Read the source file directly before changing related behavior. Tailscale is external-only here: this service may read status and parse peers, but must not start, stop, restart, or rewrite the host Tailscale runtime.

## Runtime Impact

Runtime impact is read-only. It calls the allowlisted `tailscale_status` script, reports runtime visibility, and supports inventory sync diagnostics.

## Guardrails

- Keep FWRouter core as the authority for classification and policy routing.
- Keep Mihomo as a VPN egress adapter, not the network policy engine.
- Preserve direct-safe behavior for host/control-plane traffic unless an explicit scoped contour says otherwise.
