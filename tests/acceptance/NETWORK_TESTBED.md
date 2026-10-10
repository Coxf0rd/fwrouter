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

## Minimal packet topology — implementation pending qualification

Reuse the same harness and dependency layers for three roles:

```text
LAN client -- internal LAN -- application/router -- internal WAN -- endpoint
                                Core/API
                                nftables/policy routing
                                Mihomo/Xray
```

The LAN client has no WAN attachment. Its default route points to the router.
The endpoint provides local TCP/HTTP/UDP/DNS fixtures and a synthetic VPN
endpoint. Direct and VPN traffic must produce distinguishable endpoint/native
observations. Docker creates the namespaces and links; tests do not require
SYS_ADMIN, host namespaces, privileged containers or host firewall mutation.

Use NET_ADMIN only where route/firewall operations require it. Packet capture
may justify NET_RAW in an explicitly reviewed packet profile; the normal
acceptance profile never inherits these capabilities. Resource limits, exact
network membership and cleanup ownership are checked for every role.

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
