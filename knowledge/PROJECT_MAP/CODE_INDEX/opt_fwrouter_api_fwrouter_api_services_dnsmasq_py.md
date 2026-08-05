# `/opt/fwrouter-api/fwrouter_api_services_dnsmasq.py`

## Purpose

Generated code-index entry for `/opt/fwrouter-api/fwrouter_api_services_dnsmasq.py`.

## Review Notes

Read the source file directly before changing related behavior. Check adjacent service, route, adapter, script, or systemd documentation as applicable.

## Runtime Impact

This file is part of the FWRouter source/runtime surface. Keep this card synchronized when the file responsibility, runtime side effects, boot relevance, or risk profile changes.

`reconcile_dnsmasq_rules(force_restart_reason=...)` writes domain-aware
selective routing fragments in `/etc/dnsmasq.d/` and restarts dnsmasq when
managed text changes, when the caller explicitly requests a runtime refresh, or
when the bounded nftset materialization probe asks for recovery. Full nft table
apply uses `force_restart_reason="nft_table_recreated"` because deleting and
recreating `inet fwrouter_v2` can leave dnsmasq's nftset writer bound to stale
kernel objects even when the dnsmasq config text is unchanged. DNS-runtime nft
sets are expected to be plain timeout sets because dnsmasq writes individual
IPv4 answers into them.

## Guardrails

- Keep FWRouter core as the authority for classification and policy routing.
- Keep Mihomo as a VPN egress adapter, not the network policy engine.
- Preserve direct-safe behavior for host/control-plane traffic unless an explicit scoped contour says otherwise.
