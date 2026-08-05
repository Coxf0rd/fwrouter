# NFTables

## Contract

FWRouter owns table `inet fwrouter_v2`. The table is generated and applied by backend dataplane services plus host libexec scripts. Do not hand-edit live nftables as source of truth.

Key sets include:

- `vpn_ipv4` / `vpn_ipv6`
- `direct_ipv4` / `direct_ipv6`
- `protected_ipv4` / `protected_ipv6`
- `dns_vpn_ipv4` / `dns_vpn_ipv6`
- `dns_direct_ipv4` / `dns_direct_ipv6`
- scoped subject sets for LAN/Tailscale/full-VPN contours

DNS-materialized sets use timeouts and are populated by `dnsmasq` nftset integration.

## Main Chains

- prerouting ingress classification for LAN/Tailscale clients
- DNS capture rules before VPN classification
- output chain for host/system traffic safety
- classify chains generated from the routing manifest
- redir/TProxy paths for Mihomo selective and full-VPN listeners

## Host Output Rules

Host output defaults to direct-safe. Protected destinations and control-plane dependencies are direct. FWRouter own traffic must not be routed to VPN because of `fwrouter:global`.

Explicit host/docker scoped VPN is valid only after stable subject attribution. FWRouter core traffic needs a separate technical contour if it ever needs special handling.

## Disabled Subjects

`desired_mode=disabled` is enforced in generated nftables, not only in UI state.
Subjects that have a scoped source matcher are rejected in `fwrouter_classify`.
IP-addressable Docker/LAN/Tailscale subjects are also rejected in `forward` in
both directions (`from subject` and `to subject`), and host-origin traffic to
their IP is rejected in `output`.

Docker host-network containers and host services that expose listeners are
rejected in the `input` chain by non-loopback listener port. Loopback remains
allowed for local health checks and internal dependencies. SSH, DNS/DHCP and
FWRouter control ports are skipped by this generic listener block.

For host-network Docker process egress, the renderer uses `meta skuid` only when
the inventory can attribute non-root process UIDs to the container. UID `0` is
not blocked generically because root is shared with the host control plane; root
host-network egress requires a future cgroup/eBPF contour.

The listener inventory comes from the routing manifest snapshot. The renderer
must not call Docker, systemd or `ss` directly.

## Selective Semantics

Selective mode must send only configured VPN destinations and DNS-materialized VPN destinations to VPN. Unmatched traffic stays direct. Large VPN lists are acceptable if they are loaded into nftables efficiently and updated through generated artifacts.

## Traffic Accounting Counters

Named nft counters use a slug derived from `subject_id`, where `:` and `-` become `_`.
Backend resolves these slugs against active canonical subjects before writing monthly traffic.
Missing Docker named counters are treated as stale runtime counters and skipped, because Docker inventory churn or a recent service shutdown can leave old counter names in live/generated artifacts until the next apply.

## Implementation Files

- `fwrouter_api/services/dataplane_global.py`
- `fwrouter_api/services/dataplane_nft.py`
- `fwrouter_api/services/traffic.py`
- `fwrouter_api/services/dataplane_status.py`
- `/usr/local/libexec/fwrouter/dataplane-apply.sh`
- `/usr/local/libexec/fwrouter/dataplane-check.sh`
- `/usr/local/libexec/fwrouter/dataplane-common.sh`
- `/usr/local/libexec/fwrouter/dataplane-rollback.sh`

## Risks

- Removing interval set auto-merge can break large IP/domain materialization.
- Applying direct cleanup without checking scoped VPN requirements can break active scoped paths.
- Changing marks, listener ports, or table names without updating shell and Python contracts creates drift.
- DNS capture placed after VPN classification breaks domain-aware selective routing.
- Treating a disabled host-network Docker service as IP-addressable is wrong:
  host-network containers need listener-port enforcement until a cgroup/owner
  egress contour exists.
- Blocking UID `0` for a disabled host-network container would also block root
  host services. Do not add a generic root `skuid` block.
