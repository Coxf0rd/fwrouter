# `/opt/fwrouter-api/fwrouter_api_services_subject_taxonomy.py`

## Purpose

Canonical backend registry for subject classes, external ingress providers, and explicit external client runtimes.

## Review Notes

Read the source file directly before changing related behavior. Check adjacent service, route, adapter, script, or systemd documentation as applicable.

## Runtime Impact

Groups native ingress clients, external ingress subjects, explicit runtime-contour clients, and client-plane subjects. External ingress providers are represented through neutral `EXTERNAL_INGRESS_*` taxonomy and remain lifecycle `external`.

It also exposes helper functions used by generic apply/dataplane/watchdog/scoped-egress code:

- transparent ingress subjects follow global mode and can use LAN-style nft policy;
- explicit external clients, currently Xray, use a runtime binding dispatcher and are not transparent nft policy subjects unless their registry contract says so;
- discovered external-network display rows get provider-specific system id, label, refresh mode, and collector defaults from taxonomy contracts;
- watchdog nft counter prefixes come from taxonomy instead of hard-coded provider names.

## Guardrails

- Keep FWRouter core as the authority for classification and policy routing.
- Keep Mihomo as a VPN egress adapter, not the network policy engine.
- Preserve direct-safe behavior for host/control-plane traffic unless an explicit scoped contour says otherwise.
- Keep provider names inside registries/adapters. Generic backend code should call taxonomy helpers instead of branching on concrete provider strings.
