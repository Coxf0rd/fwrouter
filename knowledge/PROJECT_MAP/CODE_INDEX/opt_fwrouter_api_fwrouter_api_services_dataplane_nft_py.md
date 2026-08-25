# `/opt/fwrouter-api/fwrouter_api/services/dataplane_nft.py`

## Purpose

Compatibility facade for FWRouter-owned `inet fwrouter_v2` nftables support.
The implementation is split by responsibility:

- `dataplane_nft_constants.py` owns table/chain names, static secure-DNS guard
  IPs, control-plane listener ports and mark derivation helpers.
- `dataplane_nft_sets.py` owns nft set rendering, manifest-only set inputs,
  scoped VPN set grouping and effective-rules artifact resolution.
- `dataplane_nft_chains.py` owns chain builders, disabled-subject guard lines,
  transparent redirect/TProxy handoff markers and output/prerouting helpers.
- `dataplane_nft_render.py` owns `render_owned_table_candidate()`.
- `dataplane_nft_artifacts.py` owns generated/current/applied/last-good artifact
  paths and atomic artifact promotion.

Keep old imports through this facade unless every caller and monkeypatch path is
deliberately migrated. Tests still patch
`fwrouter_api.services.dataplane_nft.read_effective_rules_artifact`, so the
facade render wrapper passes that loader into the renderer.

DNS runtime sets (`dns_direct_ipv4` / `dns_vpn_ipv4`) are plain timeout sets.
They intentionally do not use `interval` / `auto-merge`: dnsmasq only
materializes single IPv4 answers, and interval sets add unnecessary kernel
work and have been observed to destabilize dnsmasq under high DNS churn.

## Review Notes

Read the specific helper module before changing behavior. Check adjacent
service, route, adapter, script, or systemd documentation as applicable.

## Runtime Impact

Disabled subjects are enforced here. IP-addressable subjects are rejected from
`fwrouter_classify`, from `forward` in both directions, and from `output` for
host-origin traffic to the subject IP. Host-network Docker and host service
listeners are rejected from the `input` chain for non-loopback traffic. Generic
listener blocking skips SSH, DNS/DHCP and FWRouter control-plane ports.
Non-root host-network Docker process egress is rejected with `meta skuid`; UID
`0` is deliberately skipped because it is shared with the host control plane.

## Guardrails

- Keep FWRouter core as the authority for classification and policy routing.
- Keep Mihomo as a VPN egress adapter, not the network policy engine.
- Preserve direct-safe behavior for host/control-plane traffic unless an explicit scoped contour says otherwise.
