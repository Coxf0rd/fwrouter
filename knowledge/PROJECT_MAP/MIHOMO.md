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
- `fwrouter_api/services/mihomo_config_paths.py`
- `fwrouter_api/services/mihomo_config_inbounds.py`
- `fwrouter_api/services/mihomo_config_validation.py`
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
- A JSON `logical_profile` remains one user-visible server. Its persisted
  topology contains concrete member identities; materialization creates private
  member proxies plus one logical `fallback` group under the stable profile
  runtime name. `vpn-auto`, fixed selection, ping, and Xray handoff target the
  group, never an arbitrary first member. Members never cross profile
  boundaries.
- The fallback group is the bounded common runtime behavior for an Xray JSON
  profile whose original `leastLoad` balancer has no Mihomo equivalent. It
  provides viable-member failover, but does not claim to reproduce Xray
  least-load scheduling.
- Logical topology is persisted independently from subscription raw payload:
  logical server rows own normalized member rows and member health observations.
  Raw profile topology remains a read-only transition fallback. The server API
  projects active member and usable/total member counts; member diagnostics use
  the explicit logical-server/member endpoint.
- All subscription-derived user-facing servers are treated as logical servers:
  a single endpoint is a logical server with one member, and a structured
  profile is a logical server with multiple members. FWRouter selects the
  logical runtime target; Mihomo owns concrete member selection inside that
  target.
- FWRouter observes the effective member from Mihomo proxy state. Logical ping
  triggers the logical runtime path, reads Mihomo's selected member, probes that
  concrete member, and records logical latency from the effective member rather
  than from the lowest, average, first, or arbitrary member.
- Member health is an evidence layer backed by `logical_server_member_health`.
  It distinguishes `healthy`, `failed`, `stale`, and `unknown`; stale or unknown
  evidence is not treated as confirmed failure. Member probing remains bounded
  and diagnostic and does not compete with Mihomo's internal failover.
- Background member checks have a persisted cursor and a bounded two-member
  maintenance budget. Healthy and failed observations use separate TTLs;
  those checks update member state but never directly select `vpn-auto`.
- `vpn_auto=true` is VPN-auto membership. It keeps the logical server visible in
  the user-facing VPN-auto picker and eligible for manual/fixed selection.
- `vpn_auto_priority >= 0` is automatic eligibility. It allows selector and
  watchdog to choose the logical server automatically.
- `vpn_auto_priority < 0` is manual-only inside VPN-auto: the logical server
  stays visible in VPN-auto/global/manual target lists, but is excluded from
  automatic Mihomo/watchdog choice, automatic reselect, and automatic
  subscription/Xray auto pools.
- `vpn_auto_priority` `0..5` weights latency for auto-selection using
  direct weight semantics: `0` and `1` are 1x, `2` is 2x, up to `5` as 5x. It
  is not a strict ordering rank.
- `vpn_auto_priority_origin` records whether priority came from automatic
  defaulting, an explicit manual edit, or legacy state. Adding a default-priority
  server to VPN-auto auto-sets `0 -> 1`; removing it resets `1 -> 0` only when
  that `1` was auto-assigned. Manual priorities and runtime failover do not
  change `vpn_auto`, priority, or origin.
- An active `vpn-auto` logical server is valid only when it is still
  auto-eligible, present in the runtime selector, and has non-manual successful
  selector/watchdog/background health evidence. UI/manual ping results are
  diagnostics and must not keep a failed auto target valid.
- Automatic reselect builds runtime membership from Mihomo proxy inventory plus
  `vpn-auto` selector targets reported by controller health, so a logical target
  visible in the selector remains eligible even if the generic proxy inventory
  projection is partial.
- Watchdog failover treats confirmed TX-only upstream failure as unhealthy only
  after the existing debounce/cooldown confirmation. Real RX/response traffic
  suppresses failover even if an incidental probe returns an error.

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
