# `/opt/fwrouter-api/fwrouter_api_services_subjects.py`

## Purpose

Generated code-index entry for `/opt/fwrouter-api/fwrouter_api_services_subjects.py`.

## Review Notes

Read the source file directly before changing related behavior. Check adjacent service, route, adapter, script, or systemd documentation as applicable.

## Runtime Impact

Alias changes emit `client.alias_changed` in the same SQLite transaction as
the subject update. Audit values record alias presence only, never free-form
alias text.

This file is part of the FWRouter source/runtime surface. Keep this card synchronized when the file responsibility, runtime side effects, boot relevance, or risk profile changes.

## Guardrails

- Keep FWRouter core as the authority for classification and policy routing.
- Keep Mihomo as a VPN egress adapter, not the network policy engine.
- Preserve direct-safe behavior for host/control-plane traffic unless an explicit scoped contour says otherwise.
