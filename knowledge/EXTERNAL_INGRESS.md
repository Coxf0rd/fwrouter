# External Ingress

An external ingress provider is a user-managed network runtime that brings client traffic into FWRouter while remaining outside FWRouter lifecycle control.

It is different from an external management client:

- an external management client calls the API and changes intent;
- an external ingress provider brings traffic, identity, and runtime state into the dataplane;
- decoded provider payload must become normal client-plane subjects;
- provider service/control connectivity must remain direct/protected to avoid loops and access loss.
- its module lifecycle is `external`: FWRouter may probe/use it, but must not install, restart, reload, or rewrite the provider service.
- lifecycle actions for such a provider are not exposed through FWRouter API. FWRouter reads status, normalizes provider payloads, and syncs inventory, but does not manage the external provider runtime lifecycle.

## Provider Presets

Provider-specific defaults live in `fwrouter_api/services/external_provider_registry.py`. A preset may define provider name, module concept, generic subject type, identity fields, ingress matcher, service-traffic immunity, collector script, runtime probe and payload mapping.

Current repository defaults include a host command-probe preset for an overlay-network ingress provider. The preset is an integration contract, not an instruction for FWRouter to install or control that external runtime.

Decoded external ingress payload is treated as client traffic. It must pass through `fwrouter_classify` and subject-specific rules. Provider service/control/peer egress remains direct/protected according to the provider contract.

## Backend Taxonomy

`fwrouter_api/services/subject_taxonomy.py` is the canonical backend taxonomy for generic subject classes. `fwrouter_api/services/external_provider_registry.py` stores concrete provider capabilities.

Important groups:

- `NATIVE_INGRESS_SUBJECT_TYPES`: locally attached ingress clients, currently `lan`
- `EXTERNAL_INGRESS_SUBJECT_TYPES`: generic subject types created by external ingress providers
- `TRANSPARENT_INGRESS_CLIENT_SUBJECT_TYPES`: native + external ingress subjects that can follow global mode and use transparent LAN-style dataplane policy
- `EXPLICIT_EXTERNAL_CLIENT_SUBJECT_TYPES`: external clients with a separate explicit runtime contour
- provider-specific ingress and explicit-client contracts are exposed from `external_provider_registry.py`; generic apply/scoped/dataplane code calls taxonomy helpers and dispatches to provider adapters only at bounded runtime boundaries.
- `watchdog_nft_subject_counter_prefixes()`: derives authoritative nft counter prefixes from taxonomy; explicit runtime API traffic such as Xray client stats remains accounting, not transparent dataplane health.

Future ingress providers should extend the provider registry and, when needed, a bounded detail/storage mapper. Do not copy provider-specific conditionals into policy/apply/watchdog code.

## Provider Contract

A provider must define:

- stable provider name
- module concept name
- generic client subject type; imported subject IDs are prefixed by `connection_id`
- identity key that can resolve to an nft match key
- ingress interface or source CIDR used by dataplane
- service traffic immunity policy
- inventory source and freshness semantics
- traffic counter naming strategy

For transparent ingress providers, a subject must resolve to source IP/CIDR before it can be materialized in `fwrouter_classify`.

## Traffic Accounting

Provider client traffic is accounted through named nft counters:

- `cnt_<provider_subject_slug>_direct_tx`
- `cnt_<provider_subject_slug>_direct_rx`
- `cnt_<provider_subject_slug>_vpn_tx`
- `cnt_<provider_subject_slug>_vpn_rx`

Collectors map provider-specific counter names to canonical `subject_id` values. New providers need an explicit counter-name mapping before their traffic is considered authoritative.

## Safety Rules

- Do not bypass decoded client ingress before `fwrouter_classify`.
- Do not intercept provider service egress/control paths.
- Do not treat provider runtime state as persistent intent.
- Do not make provider-specific assumptions in generic policy code when taxonomy can express the class.
