# `/opt/fwrouter-api/fwrouter_api_routes_rules.py`

## Purpose

Generated code-index entry for `/opt/fwrouter-api/fwrouter_api_routes_rules.py`.

## Review Notes

Read the source file directly before changing related behavior. Check adjacent service, route, adapter, script, or systemd documentation as applicable.

## Runtime Impact

This file is part of the FWRouter source/runtime surface. Keep this card synchronized when the file responsibility, runtime side effects, boot relevance, or risk profile changes.

## Guardrails

- Keep FWRouter core as the authority for classification and policy routing.
- Keep Mihomo as a VPN egress adapter, not the network policy engine.
- Preserve direct-safe behavior for host/control-plane traffic unless an explicit scoped contour says otherwise.

## Rules audit note (2026-09-27)

`POST /rules/manual` accepts caller attribution and delegates changed-draft auditing to the existing typed writer. An identical draft is a true no-op; automatic/full-update operations are not audited as administrator intent.
