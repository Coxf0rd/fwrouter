# `/opt/fwrouter-api/fwrouter_api/services/subject_taxonomy.py`

## Purpose

Canonical backend taxonomy for subject classes and generic subject roles. Provider-specific external contracts are loaded from `external_provider_registry.py`.

## Review Notes

Read the source file directly before changing related behavior. Check adjacent service, route, adapter, script, or systemd documentation as applicable.

## Runtime Impact

Groups native ingress clients, external ingress subjects, explicit runtime-contour clients, and client-plane subjects. External entities use generic subject types such as `external_network_client` and `explicit_external_client`; legacy provider subject names normalize to those generic types.

It also exposes helper functions used by generic apply/dataplane/watchdog/scoped-egress code:

- transparent ingress subjects follow global mode and can use LAN-style nft policy;
- explicit external clients use a runtime binding dispatcher and are not transparent nft policy subjects unless their provider contract says so;
- discovered external-network display rows get provider-specific system id, label, refresh mode, and collector defaults from provider registry contracts;
- watchdog nft counter prefixes come from taxonomy instead of hard-coded provider names.

## Guardrails

- Keep FWRouter core as the authority for classification and policy routing.
- Keep Mihomo as a VPN egress adapter, not the network policy engine.
- Preserve direct-safe behavior for host/control-plane traffic unless an explicit scoped contour says otherwise.
- Keep provider names inside provider registries/adapters. Generic backend code should call taxonomy helpers and use `connection_id` attribution instead of branching on concrete provider strings.
