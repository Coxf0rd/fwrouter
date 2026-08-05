# 0003: Use Owned NFTables Table And Policy Routing

## Status

Accepted.

## Context

FWRouter needs a host dataplane for transparent steering that can be safely rebuilt, checked, and rolled back.

## Decision

Use owned table `inet fwrouter_v2` together with fwmark-based policy routing and table `100 fwrouter_vpn`.

## Consequences

- FWRouter logic is isolated from unrelated host firewall rules.
- The contract is predictable and checkable.
- Live kernel state does not survive reboot and requires recovery.
- Marks and table ids must stay synchronized across Python and shell code.

## Related Files

- `/opt/fwrouter-api/fwrouter_api/services/dataplane_global.py`
- `/opt/fwrouter-api/fwrouter_api/services/dataplane_nft.py`
- `/usr/local/libexec/fwrouter/dataplane-apply.sh`
- `/usr/local/libexec/fwrouter/dataplane-check.sh`
- `/usr/local/libexec/fwrouter/dataplane-rollback.sh`
- `/etc/iproute2/rt_tables.d/fwrouter.conf`
