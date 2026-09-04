# `/opt/fwrouter-api/fwrouter_api_main.py`

## Purpose

Generated code-index entry for `/opt/fwrouter-api/fwrouter_api_main.py`.

## Review Notes

Read the source file directly before changing related behavior. Check adjacent service, route, adapter, script, or systemd documentation as applicable.

The FastAPI app includes read-only operational/diagnostic endpoints for
`/api/v2/reconcile`, `/api/v2/events/recent`, and `/api/v2/diagnose`.

## Runtime Impact

This file is part of the FWRouter source/runtime surface. Keep this card synchronized when the file responsibility, runtime side effects, boot relevance, or risk profile changes.

## Guardrails

- Keep FWRouter core as the authority for classification and policy routing.
- Keep Mihomo as a VPN egress adapter, not the network policy engine.
- Preserve direct-safe behavior for host/control-plane traffic unless an explicit scoped contour says otherwise.
