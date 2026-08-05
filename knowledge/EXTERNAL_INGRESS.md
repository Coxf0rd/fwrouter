# External Ingress

An external ingress provider is a user-managed network runtime that brings client traffic into FWRouter while remaining outside FWRouter lifecycle control.

It is different from an external management client:

- an external management client calls the API and changes intent;
- an external ingress provider brings traffic, identity, and runtime state into the dataplane;
- decoded provider payload must become normal client-plane subjects;
- provider service/control connectivity must remain direct/protected to avoid loops and access loss.
- its module lifecycle is `external`: FWRouter may probe/use it, but must not install, restart, reload, or rewrite the provider service.

## Current Providers

### Tailscale

- provider: `tailscale`
- module concept: `tailscale`
- client subject type: `tailscale_node`
- subject id prefix: `tailscale-node:`
- ingress interface: `tailscale0`
- payload source CIDR: `100.64.0.0/10`
- identity: Tailscale peer IP, then node id/hostname as fallback
- service traffic policy: direct immune

Tailscale exit-node payload after decrypt on `tailscale0` is treated as client traffic. It must pass through `fwrouter_classify` and subject-specific rules. Tailscale service/control/peer egress remains immune on `oifname "tailscale0"`.

## Backend Taxonomy

`fwrouter_api/services/subject_taxonomy.py` is the canonical backend registry for this class.

Important groups:

- `NATIVE_INGRESS_SUBJECT_TYPES`: locally attached ingress clients, currently `lan`
- `MANAGED_EXTERNAL_INGRESS_PROVIDERS`: historical taxonomy name for external provider contracts, currently `tailscale`
- `MANAGED_EXTERNAL_INGRESS_SUBJECT_TYPES`: historical taxonomy name for subject types created by those providers, currently `tailscale_node`
- `TRANSPARENT_INGRESS_CLIENT_SUBJECT_TYPES`: native + external ingress subjects that can follow global mode and use transparent LAN-style dataplane policy
- `EXPLICIT_EXTERNAL_CLIENT_SUBJECT_TYPES`: external clients with a separate explicit runtime contour, currently `xray`

Future ingress providers should extend the provider registry and wire only their inventory/detail matcher. Do not copy hard-coded `tailscale_node` conditionals into policy/apply/watchdog code.

## Provider Contract

A provider must define:

- stable provider name
- module concept name
- client subject type and subject id prefix
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

The collector maps `cnt_tailscale_node_*` to `subject_id=tailscale-node:*`. New providers need an explicit counter-name mapping before their traffic is considered authoritative.

## Safety Rules

- Do not bypass decoded client ingress before `fwrouter_classify`.
- Do not intercept provider service egress/control paths.
- Do not treat provider runtime state as persistent intent.
- Do not make provider-specific assumptions in generic policy code when taxonomy can express the class.
