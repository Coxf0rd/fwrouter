# `/opt/fwrouter-api/fwrouter_api_services_full_refresh.py`

## Purpose

Runs the full operational resync pipeline: system subject sync, subject
inventory sync, Xray subject sync, rules full update and optional subscription
refresh.

## Review Notes

Read the source file directly before changing related behavior. Check adjacent service, route, adapter, script, or systemd documentation as applicable.

## Runtime Impact

Rules full update can outlive the default run-now wait window on large rule
sets. `run_full_refresh()` re-waits the returned rules job for a longer bounded
window before deciding success/failure, so the endpoint does not report a stale
`running` snapshot as a failed refresh.

## Guardrails

- Keep FWRouter core as the authority for classification and policy routing.
- Keep Mihomo as a VPN egress adapter, not the network policy engine.
- Preserve direct-safe behavior for host/control-plane traffic unless an explicit scoped contour says otherwise.
