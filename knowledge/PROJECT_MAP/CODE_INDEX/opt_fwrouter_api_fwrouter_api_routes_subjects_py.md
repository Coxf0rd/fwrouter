# `/opt/fwrouter-api/fwrouter_api_routes_subjects.py`

## Purpose

Generated code-index entry for `/opt/fwrouter-api/fwrouter_api_routes_subjects.py`.

## Review Notes

Read the source file directly before changing related behavior. Check adjacent service, route, adapter, script, or systemd documentation as applicable.

## Runtime Impact

This file is part of the FWRouter source/runtime surface. Keep this card synchronized when the file responsibility, runtime side effects, boot relevance, or risk profile changes.

## Endpoints

- `POST /api/v2/subjects/{subject_id}/mode` sets admin or user subject mode through apply mutation.
- `DELETE /api/v2/subjects/{subject_id}/mode` clears a user mode override from `subject_user_overrides` and returns the client to global inheritance; it does not change subject server overrides.

## Guardrails

- Keep FWRouter core as the authority for classification and policy routing.
- Keep Mihomo as a VPN egress adapter, not the network policy engine.
- Preserve direct-safe behavior for host/control-plane traffic unless an explicit scoped contour says otherwise.
