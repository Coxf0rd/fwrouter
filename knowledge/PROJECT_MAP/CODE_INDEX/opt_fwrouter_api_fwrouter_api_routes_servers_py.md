# `/opt/fwrouter-api/fwrouter_api_routes_servers.py`

## Purpose

Generated code-index entry for `/opt/fwrouter-api/fwrouter_api_routes_servers.py`.

## Review Notes

Read the source file directly before changing related behavior. Check adjacent service, route, adapter, script, or systemd documentation as applicable.

`GET /api/v2/servers` returns real server inventory by default. The Xray-only virtual `virtual:xray:vpn-auto` target is opt-in through `include_virtual_xray_vpn_auto=true` and must not be persisted into normal Mihomo `vpn-auto` membership.

Subject server override writes accept `actor_scope`; user-scope writes must stay blocked by backend when the admin committed subject mode is `direct` or `disabled`.

## Runtime Impact

This file is part of the FWRouter source/runtime surface. Keep this card synchronized when the file responsibility, runtime side effects, boot relevance, or risk profile changes.

## Guardrails

- Keep FWRouter core as the authority for classification and policy routing.
- Keep Mihomo as a VPN egress adapter, not the network policy engine.
- Preserve direct-safe behavior for host/control-plane traffic unless an explicit scoped contour says otherwise.
