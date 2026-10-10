# Isolated Linux Network Testbed

## Acceptance boundaries

This extends the existing Compose acceptance harness; it does not replace it.
The normal container-native profile remains non-root with all capabilities
dropped. Kernel qualification uses a separate, explicitly selected profile.

- **Container native:** real Xray/Mihomo, configuration application and native
  loaded-state readback. This does not prove packet routing.
- **Kernel dataplane:** private container network namespace, real nftables,
  TPROXY and policy routing, followed by actual client packet-path proof.
- **Debian host:** host systemd, physical interfaces, production kernel and boot
  recovery. Compose cannot establish this gate; it remains release acceptance.

The runner is GitHub-hosted Ubuntu. Debian 12 containers share its kernel.
No test imports production configuration, credentials, state or namespaces.

## First qualification gate

`kernel-preflight` is a bounded capability test, not application or packet
acceptance. It requires the reviewed kernel Compose override and validates the
stopped container before startup: root in that container only, read-only root,
all capabilities dropped except `NET_ADMIN`, no host ports/devices/socket or
host networking/PID/IPC, owned internal network, bounded CPU/RAM/PIDs/tmpfs.

In-container checks independently establish tool/package availability, exact
current-commit canonical dataplane script hashes, effective capabilities and a
network namespace distinct from the runner. Scratch nftables TPROXY and policy
route operations must be read back and removed. Missing features fail the gate;
no fake binaries, mock success, reservation bypass or suppressed validation.

`NFT_NOT_AVAILABLE` alone does not attribute a failure to a missing binary:
the application adapter also maps ScriptRunnerError to that code. Inspect
script installation, PATH, execution errors and netlink permissions separately.

## Minimal packet topology — source implemented, latest hosted attempt failed

Run `38077786323` reached the application packet test. Direct TCP/UDP/DNS proof
passed before the VPN phase failed: the endpoint's fresh UDP observation saw
router WAN `198.18.240.1`, not the required endpoint-local VIP `203.0.113.53`.
This is a real VPN UDP path failure; the peer assertion remains strict. The
capture worker stopped with 108 TCP and 6 UDP header records, but `docker cp`
could not read the router container's tmpfs capture files, so that run has no
validated exported pcap evidence. The fixed bounded exec exporter and expanded
phase diagnostics below are source changes only; they have **not been executed
in a hosted rerun**. Packet acceptance remains NOT RUN for this source update.

Reuse the same harness and dependency layers for three roles:

```text
LAN client -- internal LAN -- application/router -- internal WAN -- endpoint
                                Core/API
                                nftables/policy routing
                                Mihomo/Xray
```

The first concrete topology uses LAN `10.240.0.0/29` (router `.1`, client `.2`)
and WAN `198.18.240.0/29` (router `.1`, endpoint `.2`). Docker's IPAM gateway is
`.6` on each network. The local service VIP is `203.0.113.53/32` on endpoint
loopback. The endpoint has a return LAN route, the router an exact VIP route,
and neither has an Internet default route. The client default route is the
router LAN address. No SNAT is required. A native synthetic VLESS endpoint
provides the VPN hop: DIRECT preserves the client source address; VPN produces
the service VIP source address on the endpoint's local freedom connection.
The WAN capture proves the separate router-to-endpoint VLESS transport hop.
The endpoint address alone is insufficient proof: each phase requires actual
Core rule-counter deltas, client responses and WAN header captures.

The initial DNS fixture uses UDP `5353` and reserved `.test` names. It proves
local DNS packet forwarding and negative lookup behavior, **not** LAN DNS/53
capture, production resolvers, DHCP or IPv6. Native health requests target the
same local service VIP; a loopback health URL on the remote endpoint would
measure the wrong service and is not substituted with mock success.

The LAN client has no WAN attachment. Its default route points to the router.
The endpoint provides local TCP/HTTP/UDP/DNS fixtures and a synthetic VPN
endpoint. Direct and VPN traffic must produce distinguishable endpoint/native
observations. Docker creates the namespaces and links; tests do not require
SYS_ADMIN, host namespaces, privileged containers or host firewall mutation.

Use NET_ADMIN only where route/firewall operations require it. Packet capture
may justify NET_RAW in an explicitly reviewed packet profile; the normal
acceptance profile never inherits these capabilities. Resource limits, exact
network membership and cleanup ownership are checked for every role.

The packet profile uses the pinned Debian `libpcap0.8=1.10.3-1` API through one
source-hashed ctypes worker with two non-promiscuous outbound handles. Exact
TCP and UDP BPF filters, snap lengths (54/42 bytes), classic pcap 2.4 Ethernet
headers, and 512-packet per-flow limits are read back before readiness. The
worker reports the installed package version and observed API/library identity;
those observations are evidence, while the signed snapshot package pin and
profile worker digest are the independent source constraints. A pidfd-verified
SIGINT stop must produce a bounded final status before captures are validated
and exported. Since Docker's copy path may not see files in a container tmpfs,
the launcher has one fixed capture-only exec reader. It opens only the stopped
TCP or UDP file with `O_NOFOLLOW`, requires root-owned `0700` directory and
`0600` file identities plus the snaplen-derived maximum, then sends bounded
binary stdout to an exclusive host quarantine file. The normal pcap validator
still proves header-only records before atomic publication, and receipts retain
the original `docker cp` failure. No packet payload is retained.

Each packet phase records endpoint observation deltas, Core's exact global-VPN
UDP TProxy handoff counter when that rule is installed (and requires a positive
delta in VPN phases), and a small Mihomo semantic projection: the loaded
active config identity, full-VPN UDP listener shape, VLESS UDP capability,
selected proxy-class facts, and current transparent UDP session count. No raw
configuration, server identifiers, URLs, or credentials are included.

Internal Docker networks alone are not a DNS-leak proof. Before packet tests,
install a separate test-owned egress guard (outside Core's owned table): deny
non-fixture destinations and Docker's embedded DNS address `127.0.0.11`, use
only the local DNS fixture, and return local NXDOMAIN for unknown test names.
Prove the policy with negative TCP/UDP/DNS attempts and counters/capture.
Dependencies/images are fetched before the isolated packet phase.

## Proof and cleanup

Each application dataplane assertion combines:

1. Trigger and persistent intent through real application paths.
2. Core revision/incarnation and conditional publication outcome.
3. Canonical check/apply/readback, nft counters and policy routes.
4. Actual client TCP/UDP/DNS responses and observed egress path.
5. Negative leak checks and cleanup of owned rules, routes and processes.

Core counter evidence must come from `inet fwrouter_v2`, not the test-owned
egress guard. The initial scenario maps the terminal `fwrouter_direct` rule comment
`global direct path` to direct-classification evidence and the
`fwrouter vpn mark tcp:5204` / `udp:5205` rules in `fwrouter_vpn_full` to
global VPN classification (the selective `fwrouter_vpn` chain is separate).
The latter proves classification for TPROXY, not a nonexistent Core input-hook
counter. Endpoint observations and packet captures must independently prove
successful delivery. Guard drop counters establish only the particular
negative probe they observe; they cannot establish broad host dataplane parity.

API success alone cannot pass packet acceptance. Emergency Direct must change
the real traffic path and preserve desired VPN intent; re-entry must verify
traffic again. A rejected competing provider operation proves reservation,
not a successful selector race. A race requires a permitted independent
ordinary Core selector operation and a proven runtime/revision transition.

Artifacts contain bounded sanitized diagnostics and synthetic traffic only.
Captures must omit payloads/credentials; use IPv4 header-only capture or an
equally reviewed sanitization contract. Never publish raw configurations or
secret-bearing packet payloads. Docker's owned bridge plumbing can change
runner networking; record it separately from test commands, which operate
only inside owned container namespaces.

## Gates and extension

- L2: fail-closed profile, topology and proof-contract predicates.
- L3: application plus native/runtime and local packet integration.
- L4: bounded startup/readiness/parity smoke.
- L5: affected-domain integration regression, including recovery and fencing.
- L7: explicit manual/release failure-injection acceptance, never automatic.

DHCP, DNS capture/sets, IPv6, alternate LAN subnets, wider failure/recovery
matrices and host boot acceptance are not implicitly covered by the initial
gate. Register each concrete scenario and its actual execution status.

GHCR consumers must use immutable digest-locked source-free dependency images.
The code and canonical scripts under test always come from the current commit.
Publishing is trusted and separate; untrusted consumers have read-only access.
Measure pull/build/startup/size rather than promising zero downloads on fresh
GitHub-hosted machines.
