# `/opt/fwrouter-api/fwrouter_api_services_maintenance.py`

## Purpose

Generated code-index entry for `/opt/fwrouter-api/fwrouter_api_services_maintenance.py`.

## Review Notes

Read the source file directly before changing related behavior. Check adjacent service, route, adapter, script, or systemd documentation as applicable.

## Runtime Impact

This file is part of the FWRouter source/runtime surface. Keep this card synchronized when the file responsibility, runtime side effects, boot relevance, or risk profile changes.
The maintenance storage estimate includes stale generated temp files reported
by `state_retention.generated_tmp_files`, and real maintenance logs how many of
those files and bytes were removed.

## Guardrails

- Keep FWRouter core as the authority for classification and policy routing.
- Keep Mihomo as a VPN egress adapter, not the network policy engine.
- Preserve direct-safe behavior for host/control-plane traffic unless an explicit scoped contour says otherwise.
