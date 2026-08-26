# `/usr/local/libexec/fwrouter/dataplane_apply.sh`

## Purpose

Applies FWRouter-owned nftables candidate and policy routing contract from generated candidate/manifest files.

## Behavior Notes

- Sources `dataplane-common.sh` for manifest readers and policy-routing helpers.
- When VPN policy routing is required, optional conntrack cleanup reads source CIDRs from `extra.network_contract.trusted_client_ipv4_networks`. If an old manifest has no network contract, cleanup is skipped so shell does not become a hidden owner of deployment CIDRs.

## Review Notes

Read the source file directly before changing related behavior. Check adjacent service, route, adapter, script, or systemd documentation as applicable.

## Runtime Impact

This file is part of the FWRouter source/runtime surface. Keep this card synchronized when the file responsibility, runtime side effects, boot relevance, or risk profile changes.

## Guardrails

- Keep FWRouter core as the authority for classification and policy routing.
- Keep Mihomo as a VPN egress adapter, not the network policy engine.
- Preserve direct-safe behavior for host/control-plane traffic unless an explicit scoped contour says otherwise.
