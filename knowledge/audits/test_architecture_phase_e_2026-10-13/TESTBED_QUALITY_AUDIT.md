# Phase E Network Testbed and Test Quality Audit

Date: 2026-10-13
Branch checkpoint: `stage/test-architecture-ci-stabilization`, `3e12141c5d7be5d740450e6c13001a8fbc827f3b`
Review type: read-only source, contract, roadmap and retained hosted evidence review. No tests, application imports, native binaries, Docker or production state were executed or changed.

## Result

No blocking false-green, namespace escape, missing packet assertion, cleanup ownership defect, or stale-application-code dependency was identified in the reviewed Network Testbed. The accepted Phase D packet evidence remains valid within its declared hosted Linux-container-kernel scope. This review does not establish Debian host/systemd/reboot behavior, and does not extend DNS coverage beyond the scenarios actually run.

## Architecture and isolation

The harness separates ordinary container-native acceptance from the explicitly selected kernel packet profile. The ordinary profile drops capabilities; kernel qualification and packet testing use isolated Compose containers, bounded resources, read-only roots/profile mounts, run-owned internal IPv4 networks, no host networking/PID/IPC/devices/ports, and only the capabilities needed for the qualified role. The packet fixture independently rejects production markers, checks a namespace distinct from the runner, checks effective capabilities and the exact current-source script hashes, and reads back scratch nftables and policy-route changes. Testbed documentation explicitly distinguishes this from Debian host acceptance.

The packet topology uses an internal LAN with a client attached only to LAN and an internal WAN with the router and endpoint. Neither network has IPv6 or an Internet default path. A test-owned egress guard denies non-fixture traffic and Docker embedded DNS; negative probes and counters are required. Dependency retrieval occurs before the isolated packet phase. Thus the evidence supports isolation of the test topology, not a general claim that all Docker networking is inherently egress-proof.

## Packet and native proof

The packet scenario combines real API intent/revision operations, native Core/nftables state, policy-route/readback evidence, client responses, endpoint peer observations, WAN header-only captures, Core counter deltas, native Mihomo projection, negative leak probes, and teardown. DIRECT and VPN paths have distinct peer observations; VPN TPROXY handoffs require positive deltas. Emergency Direct is required to change the observed route while preserving VPN intent, and re-entry checks forwarding again. Protected DIRECT, empty/unavailable VPN behavior and actual selector-change traffic are retained as separately evidenced scenarios rather than inferred from API success.

LAN DNS/53 acceptance includes both global-VPN and selective-mode DNS-priority phases. In each of the two `38305978196` packet receipts, the JUnit projections record native dnsmasq/config and nftset readiness; `vpn.test` and `direct.test` TCP/UDP flows; actual endpoint peer classes (`direct_domain_*` observed from the client, `vpn_domain_*` from the service VIP); positive selective VPN mark deltas; positive direct Core path deltas; zero direct-to-VPN capture deltas; stable native/provider selection; unchanged provider API calls; restored state; and successful cleanup. The global-VPN phase separately records upstream capture, Core classification/handoff deltas, and zero direct-capture leak deltas. Run `38181603796` is earlier scoped DNS evidence and is superseded only for these added selective-priority claims by the exact two `38305978196` projections. Coverage remains bounded to the fixed synthetic domains, transports, and hosted Linux container-kernel topology; it does not establish DHCP, IPv6, production resolver behavior, DNS performance/reliability, or Debian host parity.

## Fixtures, assertions and concurrency

`tests/gates/test_network_fixture_contract.py` is extensive and checks source-level invariants for profile validation, bounded/redacted receipts, parser/response behavior, owned cleanup, role restrictions, counters/captures and the packet scenario predicates. It deliberately uses fake sockets for fixture contracts; the real end-to-end packet acceptance remains the separate hosted packet scenario, so those contract mocks are not substitutes for native proof. No observed assertion was weakened in the reviewed source. Exact current run evidence records `1 PASS / 0 FAIL / 0 SKIP` for each packet receipt and `owned_resources_removed`.

Phase D acceptance-tail changes address real test ownership concerns: ordinary component fixtures suppress unrelated prewarm admission; dedicated lifecycle tests track and join actual workers; cache invalidation tests use barriers and a hash-pinned prior-source fixture; browser/native application cases retain real application/native scheduling. The 300-case cohort ran five times in separate run `38305595288`; it is not part of final bundle `38305978196`. That final bundle independently records two clean packet receipts plus two ordinary and two provider-cohort receipts. This is evidence for the declared cases, not a claim of L6 or L7 execution.

## Source freshness and images

The final bundle identifies source commit `a10dd9c` and carries exact plan/source digests. Its hosted consumers use the digest-pinned, source-free Debian dependency base and build an application overlay from the current checkout; packet and qualified receipts validate the corresponding source/profile identity. This prevents stale application code from being silently supplied by the dependency image. The dependency layers may be cached on a runner, but GitHub-hosted jobs are independent machines; no cross-job layer-download elimination is assumed.

## Acceptance evidence and limits

`FULL_BUNDLE_CHECKPOINT_38305978196.json` records aggregate PASS, 1429/0/0 unit counts, completed L0-L4, six hosted receipts, two required executions of each ordinary/provider/packet partition, and qualified Xray 1/0/0 plus Mihomo child 26/0/0 with owned cleanup. The packet receipts contain two captures each and no skips. The contract and packet documentation retain prior failures and their scope rather than treating missing collection as PASS.

Not run by this audit: tests, Docker, native execution, L6, L7, an actual PR/main event, Debian host systemd/boot/reboot acceptance, or production behavior. These are boundaries, not defects in the Testbed assertions.

## Roadmap follow-up inventory

The canonical roadmap and mirror preserve the overall stage order and include broad production documentation and Stage 5 configuration/persistence work. Exact future requirements requested for this audit are not all present as named planned items in both active roadmaps:

- Configuration Matrix & Security Acceptance: not found by exact active-roadmap search; relate to Stage 5 without silently expanding its accepted scope.
- DNS Performance & Reliability: not found as a named future milestone/item.
- Rescue `rescue0` Network Isolation Audit: not found.
- Production Docs RU/EN with screenshots, architecture and guides: broad Production Documentation exists; these concrete deliverables are not enumerated.
- Server-Wide Cleanup & Storage Audit: not found as a named future item.
- Production Live Acceptance: operational/deployment acceptance is mentioned, but no explicit named post-deployment live acceptance gate was found in the active sequence.
- Squash and Merge policy (one final main commit per PR; auto-delete merged feature branches): not found as a named policy.
- External Integration Framework & SDK after Core/Modules stabilization: Core/Modules milestones and late telemetry exist, but this framework/SDK requirement is not present.

These are roadmap synchronization follow-ups for the milestone owner; this audit makes no roadmap edits and does not implement these future scopes.
