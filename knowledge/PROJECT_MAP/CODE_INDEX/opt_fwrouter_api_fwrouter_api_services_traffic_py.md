# `/opt/fwrouter-api/fwrouter_api_services_traffic.py`

## Purpose

Owns traffic accounting normalization, monthly delta recording, collector script parsing, and traffic history cleanup.

## Behavior Notes

- Named nft counters are normalized back to active subjects before writing snapshots/monthly rows.
- Missing Docker and host-service named counters are treated as stale runtime counters and reported as skipped/stale, not invalid samples.
- Missing LAN/external ingress/Xray subjects remain invalid because they can indicate broken attribution.
- Xray stats API samples are recorded as per-client `xray:subject:<subject_id>` traffic accounting, but are not watchdog health signals.
- External samples must declare `metadata.connection_id` or use a collector name `external_connection:{connection_id}`. Legacy `external_system_id` is no longer accepted as input identity; the backend enriches metadata with connection label/type/runtime and rejects unknown records.
- `external_management` connections cannot submit traffic samples; they are management API clients only.

## Review Notes

Read the source file directly before changing related behavior. Check adjacent service, route, adapter, script, or systemd documentation as applicable.

## Runtime Impact

This file is part of the FWRouter source/runtime surface. Keep this card synchronized when the file responsibility, runtime side effects, boot relevance, or risk profile changes.

## Guardrails

- Keep FWRouter core as the authority for classification and policy routing.
- Keep Mihomo as a VPN egress adapter, not the network policy engine.
- Preserve direct-safe behavior for host/control-plane traffic unless an explicit scoped contour says otherwise.
