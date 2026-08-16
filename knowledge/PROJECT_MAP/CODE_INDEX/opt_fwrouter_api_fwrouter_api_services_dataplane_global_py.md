# `/opt/fwrouter-api/fwrouter_api_services_dataplane_global.py`

## Purpose

Generated code-index entry for `/opt/fwrouter-api/fwrouter_api_services_dataplane_global.py`.

## Review Notes

Read the source file directly before changing related behavior. Check adjacent service, route, adapter, script, or systemd documentation as applicable.

## Runtime Impact

Builds global dataplane preflight/readiness and the `vpn_routing_contract` consumed by nft manifest rendering. The active VPN dataplane is selected through `runtime_adapters.active_vpn_dataplane_adapter()`: Mihomo is the default managed adapter, but a ready `external_vpn_module` from UI display settings can supply the transparent redir/tproxy ports for the VPN contour. External VPN modules skip Mihomo lifecycle/reconcile and do not make HTTP/SOCKS endpoints part of transparent nft routing.

## Guardrails

- Keep FWRouter core as the authority for classification and policy routing.
- Keep Mihomo as a VPN egress adapter, not the network policy engine.
- Preserve direct-safe behavior for host/control-plane traffic unless an explicit scoped contour says otherwise.
