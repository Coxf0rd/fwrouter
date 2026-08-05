# `/opt/fwrouter-api/fwrouter_api_services_apply.py`

## Purpose

Generated code-index entry for `/opt/fwrouter-api/fwrouter_api_services_apply.py`.

## Review Notes

Read the source file directly before changing related behavior. Check adjacent service, route, adapter, script, or systemd documentation as applicable.

## Runtime Impact

This file is part of the FWRouter source/runtime surface. Keep this card synchronized when the file responsibility, runtime side effects, boot relevance, or risk profile changes.

After a successful full `nft` apply for domain-aware selective rules,
`run_apply_pipeline()` calls `reconcile_dnsmasq_rules(force_restart_reason="nft_table_recreated")`.
The forced restart refreshes dnsmasq's live nftset writer after `inet
fwrouter_v2` was deleted and recreated. Global/subject hot-swap paths do not
force this restart because they replace only `fwrouter_classify` and preserve
the existing nft sets.

## Guardrails

- Keep FWRouter core as the authority for classification and policy routing.
- Keep Mihomo as a VPN egress adapter, not the network policy engine.
- Preserve direct-safe behavior for host/control-plane traffic unless an explicit scoped contour says otherwise.
