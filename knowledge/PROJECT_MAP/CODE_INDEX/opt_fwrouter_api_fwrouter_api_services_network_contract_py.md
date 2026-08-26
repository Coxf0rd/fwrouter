# `/opt/fwrouter-api/fwrouter_api_services_network_contract.py`

## Purpose

Owns the normalized FWRouter network contract for deployment-specific CIDR/interface/hostname bindings.

## Behavior Notes

- Reads JSON array/object values from `FWROUTER_*` settings; deployments can supply any valid CIDR/interface names.
- Provides protected IPv4/IPv6 ranges for dataplane sets.
- Provides rules-only extra protected ranges for the rules compiler.
- Provides trusted client IPv4/IPv6 ranges for nft transparent ingress guards and shell conntrack cleanup.
- Provides LAN interface allow/deny filtering and local LAN hostnames for dnsmasq.
- Exposes `network_contract_manifest()` so host scripts consume the same contract as Python renderers.

## Runtime Impact

This file does not write state. It affects generated nftables, effective protected rules, dnsmasq fragments, and apply-time conntrack cleanup.

## Guardrails

- Keep invalid/empty protected/trusted lists fail-safe by falling back to defaults.
- Do not duplicate network CIDR literals in renderer or shell scripts when the unified contract can carry them.
