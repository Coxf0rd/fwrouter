# Troubleshooting

## Basic Checks

```bash
/opt/fwrouter-api/scripts/check_boot_persistence.sh
systemctl --failed
journalctl -u fwrouter-api.service -u fwrouter-mihomo.service -u fwrouter-xray.service -u fwrouter-xray-sub-gateway.service -n 200 --no-pager
```

## DNS Looks Wrong

```bash
host 2ip.ua 127.0.0.1
host 2ip.ua 1.1.1.1
cat /etc/resolv.conf
journalctl -u dnsmasq -n 100 --no-pager
iptables -t nat -L PREROUTING -v -n --line-numbers | sed -n '1,20p'
```

Expected contract:

- LAN clients must use the router DNS address for the current deployment.
- FWRouter owns LAN DNS advertisement through `/etc/dnsmasq.d/fwrouter-dhcp-dns.conf`.
- Public secondary DNS in DHCP breaks domain-aware selective routing.
- `dnsmasq` should use public upstreams from `fwrouter-upstream-dns.conf`, not ISP DNS injected through DHCP.
- Domain-aware selective routing requires DNS capture for LAN `53/tcp` and `53/udp` before VPN classification.
- Secure DNS bypass must be blocked for common DoH/DoT resolvers when domain-aware selective routing is expected.

If local `127.0.0.1` returns `NXDOMAIN` but a public resolver answers, inspect the upstream resolver chain and `dnsmasq` logs.

For live `dnsmasq 2.90`, the LAN IPv4-only nftset format should use separate family entries such as `nftset=/domain/4#inet#fwrouter_v2#dns_vpn_ipv4`. Combined IPv4/IPv6 entries can fail to materialize sets.

Runtime DNS IPs must land in timeout sets `dns_vpn_ipv4` and `dns_direct_ipv4`, not persistent `vpn_ipv4` and `direct_ipv4` sets.

## Mihomo Does Not Start

Check:

```bash
ls -l /dev/net/tun
docker ps
ss -ltnup | grep 5200
docker logs --tail 80 fwrouter-mihomo
grep -nA5 -B2 'fwrouter-tproxy' /var/lib/fwrouter-v2/generated/mihomo/config.yaml
```

Known signals:

- `unsupported rule type: SRC-IP` means old source rules reached Mihomo. Use `SRC-IP-CIDR,<client>/32,vpn-global` only where source rules are still expected.
- `fwrouter-tproxy` must bind to `0.0.0.0` or the LAN address, not only `127.0.0.1`.
- If explicit proxy `:5201` works but transparent client traffic does not, inspect listener binding and nft redirect/TProxy rules first.

## Xray Does Not Start

Check:

```bash
docker network inspect "${FWROUTER_DOCKER_PROXY_NETWORK:-fwrouter_proxy}"
cat /var/lib/fwrouter-v2/xray/config.json
docker logs --tail 100 fwrouter-xray
journalctl -u fwrouter-xray.service -u fwrouter-xray-sub-gateway.service -n 200 --no-pager
```

The configured Docker network is an external unit dependency and must exist before `fwrouter-xray.service` starts. The installer creates it only when a managed runtime component is selected; default `fwrouter_proxy`, legacy override `FWROUTER_DOCKER_PROXY_NETWORK=proxy_net`.

## Routing Does Not Match Intent

Check:

```bash
curl -s http://127.0.0.1:5000/api/v2/runtime | jq .
curl -s http://127.0.0.1:5000/api/v2/routing/global | jq .
ip rule show
ip route show table all
nft list ruleset
```

Inspect generated and promoted artifacts:

- `/var/lib/fwrouter-v2/generated/dataplane/`
- `/var/lib/fwrouter-v2/last-good/`
- applied manifest files under the latest job/artifact paths

In pure `global=direct` without scoped VPN users, policy-routing rules for FWRouter VPN marks should be absent and table `100` should be empty.

In `global=direct` with active scoped `selective` or `vpn` users, policy-routing may remain valid because scoped transparent ingress still needs table `100`.

## Selective Routing Is Too Broad

Selective mode should send only matched VPN domains/IPs to Mihomo. It must not send every non-direct destination to VPN.

Check:

```bash
nft list set inet fwrouter_v2 vpn_ipv4
nft list set inet fwrouter_v2 direct_ipv4
nft list set inet fwrouter_v2 dns_vpn_ipv4
nft list set inet fwrouter_v2 dns_direct_ipv4
nft list chain inet fwrouter_v2 fwrouter_classify
```

If most unmatched traffic reaches VPN, inspect the generated manifest fallback and Mihomo `MATCH` behavior. The FWRouter core owns policy classification; Mihomo must stay an egress adapter.

## Runtime Convergence Load

`runtime_convergence_scheduler` should skip heavy `dnsmasq` reconcile when selective status is healthy. Healthy scans record `dnsmasq.skipped=true` and `preflight_action=skip_reconcile_status_ok`. If every minute restarts or rewrites DNS artifacts, inspect `inspect_dnsmasq_selective_status()` output before changing the heavy contour.

## Boot Persistence

```bash
/opt/fwrouter-api/scripts/check_boot_persistence.sh
ip rule show
ip route show table 100
nft list ruleset | grep fwrouter_v2
```

After reboot, backend startup recovery should restore intended routing from SQLite and generated/last-good artifacts. Missing live nftables or policy rules immediately after cold boot can be normal only before recovery completes; the host must remain in direct-safe bootstrap mode during that window.
