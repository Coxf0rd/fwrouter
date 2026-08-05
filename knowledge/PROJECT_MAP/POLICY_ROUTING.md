# Policy Routing

## Canonical Contract

- routing table name: `fwrouter_vpn`
- routing table id: `100`
- primary rule priority: `100`
- full-VPN UDP rule priority: `101`
- primary UDP/TProxy fwmark: `0x00000100` (`256`)
- full-VPN UDP/TProxy fwmark: `0x00000102` (`258`)
- bypass mark: `0x00000200` (`512`)
- route target: `local default dev lo`
- selective TCP listener: `5202`
- selective UDP listener: `5203`
- full-VPN TCP listener: `5204`
- full-VPN UDP listener: `5205`

TCP redirect packets use separate marks and must not be matched by policy-routing rules intended for UDP/TProxy.

## Implementation Files

- `/etc/iproute2/rt_tables.d/fwrouter.conf`
- `fwrouter_api/services/dataplane_global.py`
- `/usr/local/libexec/fwrouter/dataplane-apply.sh`
- `/usr/local/libexec/fwrouter/dataplane-check.sh`
- `/usr/local/libexec/fwrouter/dataplane-common.sh`
- `/usr/local/libexec/fwrouter/dataplane-rollback.sh`

## Boot Persistence

The `rt_tables.d` fragment is persistent. Live `ip rule` and `ip route` state is not persistent and must be recreated after reboot by backend startup recovery.

## Idempotency

`dataplane-apply.sh` and `dataplane-rollback.sh` loop-delete legacy rules before replacing routes. Keep this behavior to avoid duplicate rules.

`dataplane-apply.sh` and `dataplane-check.sh` must read the manifest routing contract through the same shared helper/fallback order. Otherwise apply and check can diverge on mixed-era artifacts.

## Direct Cleanup

When switching to direct mode, policy routing is removed only if the candidate does not require transparent VPN routing.

- If `summary.requires_vpn_policy_routing=false`, VPN mark rules should be absent and table `100` should not contain `local default dev lo`.
- If `global_mode=direct` but active scoped selective/vpn subjects still require transparent ingress, policy routing must remain installed.

## Checks

```bash
ip rule show
ip route show table 100
grep -R . /etc/iproute2/rt_tables.d
```

Expected states:

- `selective` and `vpn`: fwmark rules for `0x100` and `0x102` plus `local default dev lo` in table `100`.
- pure `direct`: no FWRouter VPN mark rules and no table `100` local default route.
- `direct + scoped selective/vpn`: rules may remain if manifest requires transparent VPN policy routing.

## Risks

- Changing table id or marks breaks generated artifacts and runtime checks.
- Duplicate `ip rule` entries create unpredictable steering.
- Missing `src_valid_mark=1` breaks marked routing semantics.
