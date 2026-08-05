# `/opt/fwrouter-api/fwrouter_api_services_runtime_convergence.py`

## Purpose

Owns the runtime self-heal loop for selective/VPN dataplane contracts. It checks live dataplane drift first, then dnsmasq selective nftset readiness, and calls the existing repair entrypoints when needed.

## Behavior Notes

- Scheduler calls are cached briefly to avoid duplicate expensive probes.
- Dataplane drift repair runs before dnsmasq reconciliation. If dataplane repair fails, dnsmasq reconciliation is skipped because nftset probes are not meaningful against a broken owned table.
- Pure dnsmasq active-probe misses are treated as transient probes, not as repair triggers, when the rest of the DNS contract is healthy. This covers nftset materialization misses and single-domain resolve failures when another probe succeeds. Full nft table apply still forces dnsmasq refresh separately, and all-probe resolve failure still uses the repair path.
- After repeated identical repair failures, non-forced scheduler calls enter cooldown and return `status: cooldown` without running dnsmasq/dataplane repair again.
- `force=True` bypasses cooldown for manual diagnostics or explicit repair.
- Successful convergence resets the failure counter and cooldown state.

## Review Notes

Read the source file directly before changing related behavior. Check adjacent service, route, adapter, script, or systemd documentation as applicable.

## Runtime Impact

This file is part of the FWRouter source/runtime surface. Keep this card synchronized when the file responsibility, runtime side effects, boot relevance, or risk profile changes.

## Guardrails

- Keep FWRouter core as the authority for classification and policy routing.
- Keep Mihomo as a VPN egress adapter, not the network policy engine.
- Preserve direct-safe behavior for host/control-plane traffic unless an explicit scoped contour says otherwise.
