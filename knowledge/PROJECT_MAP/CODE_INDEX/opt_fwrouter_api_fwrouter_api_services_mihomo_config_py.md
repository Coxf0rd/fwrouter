# `/opt/fwrouter-api/fwrouter_api_services_mihomo_config.py`

## Purpose

Generated code-index entry for `/opt/fwrouter-api/fwrouter_api_services_mihomo_config.py`.

## Review Notes

Read the source file directly before changing related behavior. Check adjacent service, route, adapter, script, or systemd documentation as applicable.

## Runtime Impact

This file is part of the FWRouter source/runtime surface. Keep this card synchronized when the file responsibility, runtime side effects, boot relevance, or risk profile changes.

`reconcile_mihomo_selective_default_fast(...)` is the narrow fast path for
`selective_default` toggles. It patches only the FWRouter-owned transparent
fallback and `fwrouter` metadata, restarts managed Mihomo, and falls back to the
full reconcile path if the active config shape is not exactly recognized.

## Guardrails

- Keep FWRouter core as the authority for classification and policy routing.
- Keep Mihomo as a VPN egress adapter, not the network policy engine.
- Preserve direct-safe behavior for host/control-plane traffic unless an explicit scoped contour says otherwise.
- Subject server overrides must target stable `fwrouter-subject-*` Mihomo selectors. Selective mode source-scopes only VPN-matching `fwrouter-transparent` rules to that selector, and full-VPN mode source-scopes `fwrouter-full-vpn`; do not point source rules directly at concrete servers.
- A pure `selective_default` change must not rebuild the full large Mihomo rules inventory when a fallback-only patch is structurally safe.
