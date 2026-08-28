# `/opt/fwrouter-api/fwrouter_api/services/runtime.py`

## Purpose

Runtime summary builder for backend, dataplane, modules, subscriptions, scoped egress, and external ingress probes.

## Review Notes

Read the source file directly before changing related behavior. Check adjacent service, route, adapter, script, or systemd documentation as applicable.

## Runtime Impact

Runtime summary is read-only. External ingress runtime probes are built only from enabled `external_network_source` records in the persistent `external_connections` registry and are keyed by `connection_id`; provider capability alone does not create runtime/cache state.

## Guardrails

- Keep FWRouter core as the authority for classification and policy routing.
- Keep Mihomo as a VPN egress adapter, not the network policy engine.
- Preserve direct-safe behavior for host/control-plane traffic unless an explicit scoped contour says otherwise.
- Do not probe provider-specific external ingress runtimes unless a concrete registered connection exists.
