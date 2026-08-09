# `/opt/fwrouter-api/fwrouter_api_adapters_mihomo.py`

## Purpose

Generated code-index entry for `/opt/fwrouter-api/fwrouter_api_adapters_mihomo.py`.

## Review Notes

Read the source file directly before changing related behavior. Check adjacent service, route, adapter, script, or systemd documentation as applicable.

## Runtime Impact

This file is part of the FWRouter source/runtime surface. Keep this card synchronized when the file responsibility, runtime side effects, boot relevance, or risk profile changes.

## Guardrails

- Keep FWRouter core as the authority for classification and policy routing.
- Keep Mihomo as a VPN egress adapter, not the network policy engine.
- Preserve direct-safe behavior for host/control-plane traffic unless an explicit scoped contour says otherwise.
- Selector hot paths must use targeted `/proxies/{name}` controller calls for target/selected-state checks. Avoid full `/proxies` reads when switching subject/global selectors; large rule sets make that endpoint expensive.
- Cache the controller `secret` by generated-config mtime for request headers. Re-reading the large generated config for every local controller request is too expensive for selector switching.
- Controller `404` from targeted selector endpoints is normalized to `MIHOMO_SELECTOR_NOT_FOUND`, not generic apply failure. Subject override clear paths rely on this to remove stale/pending DB overrides when the runtime selector is no longer materialized.
