# `/opt/fwrouter-api/fwrouter_api_services_scoped_egress.py`

## Purpose

Generated code-index entry for `/opt/fwrouter-api/fwrouter_api_services_scoped_egress.py`.

## Review Notes

Read the source file directly before changing related behavior. Check adjacent service, route, adapter, script, or systemd documentation as applicable.

## Runtime Impact

Builds scoped-egress runtime state for subjects: matcher resolution, applied/pending status, explicit client runtime bindings, and resolved VPN target projection.

Transparent ingress subjects use taxonomy contracts to resolve LAN-style nft matchers. Explicit client subjects, currently Xray, use a runtime binding dispatcher and are materialized from their bindings file instead of generic nft classify.

## Guardrails

- Keep FWRouter core as the authority for classification and policy routing.
- Keep Mihomo as a VPN egress adapter, not the network policy engine.
- Preserve direct-safe behavior for host/control-plane traffic unless an explicit scoped contour says otherwise.
- Selective LAN/external ingress subjects with a server override are applied when the transparent selective runtime is materialized; only direct-only subjects stay `pending_not_vpn_path`.
- Keep concrete provider details inside matcher/runtime resolvers. Generic scoped-egress decisions should use `subject_taxonomy` helpers.
