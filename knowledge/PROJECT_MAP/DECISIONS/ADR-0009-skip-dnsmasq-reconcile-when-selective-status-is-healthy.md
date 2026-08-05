# ADR-0009: Skip Dnsmasq Reconcile When Selective Status Is Healthy

## Status

Accepted

## Context

`global selective` uses `dnsmasq` as the DNS materialization adapter: domain rules populate nft sets, while `fwrouter` remains the routing authority.

`runtime_convergence_scheduler` runs regularly to detect and repair drift. Before this decision, each convergence tick in selective/VPN scope could enter full `dnsmasq` reconcile. That path reads the full `effective-rules.json`, walks the large domain ruleset, renders dnsmasq config, probes nftset materialization and may restart `dnsmasq`.

For large `big_vpn` lists this is too expensive for a periodic health loop when the live selective contract is already healthy.

## Decision

Runtime convergence must first run the cheap selective DNS status check.

If `inspect_dnsmasq_selective_status()` returns `ok=true`, convergence records `dnsmasq.skipped=true` with `preflight_action=skip_reconcile_status_ok` and does not call full `reconcile_dnsmasq_rules()`.

If the status is unhealthy, or status inspection itself fails, convergence still runs full `reconcile_dnsmasq_rules()` as the repair path.

## Consequences

Benefits: regular convergence no longer reads/renders the large rules artifact when DNS selective state is healthy.  
Costs: health status correctness becomes the gate for skipping reconcile.  
Risks: if the cheap status check misses drift, full reconcile is delayed until another signal reports unhealthy state or an explicit apply/reconcile runs.

## Related Files

- `/srv/fwrouter/backend/fwrouter_api/services/runtime_convergence.py`
- `/srv/fwrouter/backend/fwrouter_api/services/dnsmasq.py`
- `/srv/fwrouter/backend/tests/test_watchdog.py`
