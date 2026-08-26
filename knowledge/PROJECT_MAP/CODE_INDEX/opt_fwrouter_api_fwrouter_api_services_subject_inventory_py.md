# `/opt/fwrouter-api/fwrouter_api_services_subject_inventory.py`

## Purpose

Synchronizes discovered LAN, external ingress, Xray, Docker and host subjects into
SQLite. Docker discovery prefers the enriched `docker_inventory` helper, which
adds container network mode, bridge IPs, published ports and host-network
listeners plus process UID metadata; it falls back to legacy `docker_ps` when
the helper is unavailable.
Host discovery accepts listener metadata from `host-services.py`.
External ingress discovery uses `external_ingress.py` to normalize provider payloads
from registry contracts instead of provider-specific service modules.

## Review Notes

Read the source file directly before changing related behavior. Check adjacent service, route, adapter, script, or systemd documentation as applicable.

## Runtime Impact

The detail tables keep stable columns, while richer runtime attribution is
stored in `source_json`. That metadata is later bounded by the dataplane manifest
and used to enforce disabled Docker/host subjects, including non-root
host-network Docker egress when UID attribution is safe.

## Guardrails

- Keep FWRouter core as the authority for classification and policy routing.
- Keep Mihomo as a VPN egress adapter, not the network policy engine.
- Preserve direct-safe behavior for host/control-plane traffic unless an explicit scoped contour says otherwise.
