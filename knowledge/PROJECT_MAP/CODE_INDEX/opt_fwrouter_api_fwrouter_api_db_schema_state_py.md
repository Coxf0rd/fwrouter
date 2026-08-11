# `/opt/fwrouter-api/fwrouter_api_db_schema_state.py`

## Purpose

Schema drift inspection for the SQLite control-plane database. Current expected schema version is `9`.

## Review Notes

Read the source file directly before changing related behavior. Check adjacent service, route, adapter, script, or systemd documentation as applicable.
The contract expectations include `subjects.subject_role` and `subjects.implementation_kind`; this catches databases that still only expose concrete `subject_type` without generic inventory roles.

## Runtime Impact

This file is part of the FWRouter source/runtime surface. Keep this card synchronized when the file responsibility, runtime side effects, boot relevance, or risk profile changes.

## Guardrails

- Keep FWRouter core as the authority for classification and policy routing.
- Keep Mihomo as a VPN egress adapter, not the network policy engine.
- Preserve direct-safe behavior for host/control-plane traffic unless an explicit scoped contour says otherwise.
