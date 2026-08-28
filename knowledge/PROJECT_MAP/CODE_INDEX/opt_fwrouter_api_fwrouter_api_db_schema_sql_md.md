# `/opt/fwrouter-api/fwrouter_api_db_schema.sql`

## Purpose

Canonical SQLite schema definition. Current schema version is `12`.

## Review Notes

Read the source file directly before changing related behavior. Check adjacent service, route, adapter, script, or systemd documentation as applicable.
`subjects.subject_role` is the generic role used by UI/API grouping and policy-facing read models; `subjects.implementation_kind` keeps the concrete implementation/adapter. `subjects.subject_type` remains the detail/runtime storage key for existing specialized tables and is intentionally not constrained by a provider enum CHECK.

## Runtime Impact

This file is part of the FWRouter source/runtime surface. Keep this card synchronized when the file responsibility, runtime side effects, boot relevance, or risk profile changes.
Clean DB seeds only core module rows (`core`, `vpn`, `watchdog`, `selector`, `subscription`). Optional provider/runtime rows such as `xray` and `tailscale` must not be pre-created by schema bootstrap.

## Guardrails

- Keep FWRouter core as the authority for classification and policy routing.
- Keep Mihomo as a VPN egress adapter, not the network policy engine.
- Preserve direct-safe behavior for host/control-plane traffic unless an explicit scoped contour says otherwise.
