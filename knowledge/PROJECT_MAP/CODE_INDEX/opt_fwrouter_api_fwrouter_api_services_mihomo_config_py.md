# `/opt/fwrouter-api/fwrouter_api_services_mihomo_config.py`

## Purpose

Generated code-index entry for `/opt/fwrouter-api/fwrouter_api_services_mihomo_config.py`.

## Review Notes

Read the source file directly before changing related behavior. Check adjacent service, route, adapter, script, or systemd documentation as applicable.

## Runtime Impact

This file builds and validates Mihomo config and remains the compatibility
facade for old imports. Runtime promote/reconcile logic lives in
`mihomo_reconcile.py` and is re-exported here.
Candidate config writes use the shared artifact `atomic_write_text()` helper,
so failed writes leave standard `.tmp` files that state retention can identify
instead of ad-hoc `tmp*` files in `generated/mihomo`.

Keep this card synchronized when builder/validator/facade responsibility,
runtime side effects, boot relevance, or risk profile changes.

## Guardrails

- Keep FWRouter core as the authority for classification and policy routing.
- Keep Mihomo as a VPN egress adapter, not the network policy engine.
- Preserve direct-safe behavior for host/control-plane traffic unless an explicit scoped contour says otherwise.
- Subject server overrides must target stable `fwrouter-subject-*` Mihomo selectors. Selective mode source-scopes only VPN-matching `fwrouter-transparent` rules to that selector, and full-VPN mode source-scopes `fwrouter-full-vpn`; do not point source rules directly at concrete servers.
- Reconcile functions are re-exported for compatibility; new ownership belongs in `mihomo_reconcile.py`.
