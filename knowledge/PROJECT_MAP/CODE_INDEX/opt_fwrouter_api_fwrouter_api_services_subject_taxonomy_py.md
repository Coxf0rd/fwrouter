# `/opt/fwrouter-api/fwrouter_api_services_subject_taxonomy.py`

## Purpose

Canonical backend registry for subject classes and external ingress provider taxonomy.

## Review Notes

Read the source file directly before changing related behavior. Check adjacent service, route, adapter, script, or systemd documentation as applicable.

## Runtime Impact

Groups native ingress clients, external ingress subjects, explicit runtime-contour clients, and client-plane subjects. The `MANAGED_EXTERNAL_*` names are historical taxonomy labels and do not mean `modules.lifecycle_mode=managed`; Tailscale remains lifecycle `external`.

## Guardrails

- Keep FWRouter core as the authority for classification and policy routing.
- Keep Mihomo as a VPN egress adapter, not the network policy engine.
- Preserve direct-safe behavior for host/control-plane traffic unless an explicit scoped contour says otherwise.
