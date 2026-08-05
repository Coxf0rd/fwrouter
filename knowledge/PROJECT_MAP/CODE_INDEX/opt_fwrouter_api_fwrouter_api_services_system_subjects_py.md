# `/opt/fwrouter-api/fwrouter_api_services_system_subjects.py`

## Purpose

Owns builtin system subjects, enrichment helpers, sync-request paths, and tombstone semantics.

## Behavior Notes

- Ensures canonical `fwrouter:global` and builtin management subjects exist.
- Keeps `fwrouter:global` direct-safe and non-deletable.
- Preserves `apply_state=pending` and `applied_mode=NULL` for `fwrouter:global` after normalized control-plane imports until a real apply/verify completes.

## Review Notes

Read the source file directly before changing related behavior. Check adjacent service, route, adapter, script, or systemd documentation as applicable.

## Runtime Impact

This file is part of the FWRouter source/runtime surface. Keep this card synchronized when the file responsibility, runtime side effects, boot relevance, or risk profile changes.

## Guardrails

- Keep FWRouter core as the authority for classification and policy routing.
- Keep Mihomo as a VPN egress adapter, not the network policy engine.
- Preserve direct-safe behavior for host/control-plane traffic unless an explicit scoped contour says otherwise.
