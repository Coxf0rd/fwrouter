# Mihomo

## Role

Mihomo is the VPN egress adapter for transparent traffic. FWRouter owns classification and policy routing; Mihomo should not become the network policy engine.

Mihomo receives already steered traffic and sends it to the selected proxy group/server. Domain-aware rules may be generated into Mihomo for post-sniffing behavior, but the architectural authority remains FWRouter core.

## Main Files

- `/opt/fwrouter-mihomo/docker-compose.yml`
- `/var/lib/fwrouter-v2/generated/mihomo/config.yaml`
- `/var/lib/fwrouter-v2/generated/mihomo/config.next.yaml`
- `/var/lib/fwrouter-v2/generated/mihomo/contours.json`
- `fwrouter_api/services/mihomo_config.py`
- `fwrouter_api/adapters/mihomo.py`
- `fwrouter_api/services/mihomo_runtime.py`

## Runtime Contract

- controller: `127.0.0.1:5200`
- mixed listener: `5201`
- selective TCP listener: `fwrouter-redir` on `5202`
- selective UDP listener: `fwrouter-tproxy` on `5203`
- full-VPN TCP listener: `fwrouter-full-redir` on `5204`
- full-VPN UDP listener: `fwrouter-full-tproxy` on `5205`
- selector group: `vpn-global`
- fallback selector target: `vpn-auto`

Selective listeners route through `rule: fwrouter-transparent`. Full-VPN listeners go directly to `proxy: vpn-global` and do not use normal selective rules.

`sub-rules["fwrouter-transparent"]` reapplies domain-aware rules after sniffing. In selective mode, fallback must be direct unless the selected profile explicitly requires VPN fallback. Selective mode must not send every non-direct destination to VPN.

Scoped LAN/Tailscale full-VPN subjects are selected in nftables through the full-VPN contour, not by adding broad source-CIDR rules into Mihomo sub-rules.

## Server Selection

- Runtime `proxies` include active servers with `global_list=1` or `vpn_auto=1`.
- `vpn-auto` contains auto candidates plus `DIRECT`.
- `vpn-global` contains `vpn-auto`, manual global-list targets, and `DIRECT`.
- `vpn_auto_priority < 0` excludes a server from automatic Mihomo/watchdog choice even when it remains visible for broader inventory or Xray diagnostics.

## Diagnostics

Runtime diagnostics must distinguish TCP and UDP readiness separately:

- transparent listener presence
- transparent listener readiness
- transparent session materialization
- controller health
- selector state

A ready UDP TProxy listener alone is not enough to prove the LAN/Tailscale selective web path is healthy.

## Boot Relevance

- `fwrouter-mihomo.service` depends on network and Docker readiness.
- `/dev/net/tun` must exist before startup.
- Generated config must be valid before container start/restart.
- Backend startup may restore selector state after restart.

## Risks

- Treating Mihomo as the policy owner creates route drift and hides FWRouter classification bugs.
- Missing `/dev/net/tun` breaks the runtime.
- Selector drift can route traffic through the wrong server until recovery.
- Incorrect fallback in selective mode can send too much traffic to VPN.
- Rewriting config on every poll can create avoidable service churn.
