# Network Runtime Stability — Bugfix V1

**Status: PLANNED.** Companion to the [FWRouter roadmap](ROADMAP.md). The existing VPN degradation/recovery priority and release gates govern scheduling. Planning does not authorize implementation, qualification on Stable or deployment.

## November 19, 2026 — evidence and open questions

- **Confirmed:** The scheduled refresh began at 15:00:30 UTC+3. Both sources fetched successfully, inventory synchronized and Mihomo configuration validation passed. An internal Mihomo candidate was promoted at 15:01:11 and Xray binding materialization reported success at 15:01:25. Docker evidence confirms Xray restarted at 15:01:39. The refresh failed at the Xray generation stage with `VPN_AUTO_SELECTED_TARGET_NOT_APPLIED` after about 72.5 seconds of runtime application. The final job recorded `runtime_verified=false`, `promoted=false` and `last_good_retained=true`. Internal candidate promotion and final publication are distinct.
- **Open:** The failed VPN Auto predicate was not retained. Candidate membership or mapping, selector readback during an incarnation change, health freshness and concurrent revision changes remain hypotheses. Earlier 15/15 HTTP and 20/20 DNS checks do not establish health during the failed selection.
- **Open:** The staged path can force-recreate Mihomo when candidate bytes change, while ordinary reconcile supports semantic comparison. Incident evidence does not establish the exact number or necessity of recreations, or whether the Xray restart belonged to forward application or rollback. The retained last-good marker does not establish physical restoration. LAN interruption and continuity were not measured.
- **Code references:** `subscription_pipeline.py::_maybe_select_vpn_auto_after_refresh`, `selector.py::get_vpn_auto_state`, `xray_subscription_service.py::_apply_staged_mihomo_candidate` and `_restore_xray_generation_checkpoint`, `mihomo_reconcile.py::_semantic_config_files_equal`, and `adapters/xray_real.py::apply_staged_subscription_generation` under `backend/fwrouter_api/`.

## Architecture decisions

- Keep source fetch and inventory publication outcomes separate from runtime application and verification. Preserve successful source outcomes when runtime application fails. Keep inventory generation admission fences intact.
- Separate VPN Auto target eligibility, actual selector application and health. Use shared health-check ownership and cached observations keyed to the relevant target and check context. Bound probe intervals, deduplicate concurrent requests and apply backoff. Preserve `healthy`, `unknown` and `unhealthy` as distinct states. Unknown alone triggers neither failover nor repeated probes. Maintain fresh evidence where required and preserve fail-closed policy.
- Compare effective Mihomo and Xray configurations before applying. Semantic NOOP must produce no container recreation or process restart. Use verified targeted updates when supported. Prioritize established connections and apply structural changes only when necessary.
- Roll back affected components only and verify actual runtime state before declaring recovery complete. Keep last-known-good artifacts and ownership evidence until verification succeeds. Reuse lifecycle owners, locks, revision checks and native readback.
- **SUCCESS** requires all mandatory outcomes and runtime verification. **PARTIAL/DEGRADED** identifies completed work with unresolved or degraded runtime, including retained last-good when recovery is verified. **FAILED** identifies an operation blocked or unsuccessful without a verified safe outcome where one is required. Report source, inventory, runtime and recovery outcomes separately. Existing caller and deployment acceptance gates remain authoritative.

## B1 — Subscription Refresh & VPN Auto

**Depends on:** existing subscription and selector contracts and a reproducible isolated refresh fixture.

- Diagnose the five predicates independently: selector success, selected ID in the eligible pool, saved active ID matching the selected ID, active target/auto-health validity, and exact effective runtime target matching the expected target. Record bounded, redacted diagnostics correlated with revision and runtime incarnation.
- Separate fetched and synchronized data from runtime application. Use existing probe ownership and result sources to implement cache reuse, interval limits, in-flight deduplication and backoff.

**Acceptance:** Fixtures independently fail each of the five predicates and identify it in diagnostics. A refresh with successful fetch and failed application preserves the successful source outcomes and cannot report SUCCESS. Concurrent requests for the same check yield at most one in-flight probe. With a fake clock, cache reuse, interval and backoff prevent extra probe calls. Unknown alone causes zero failovers and zero repeated probes before a bounded check is due.

**B1 source scope:** The refresh result carries separate inventory, runtime and recovery facts. If an eligible current selector matches readback and health is unresolved, preflight defers runtime application. If application already started, staged publication stays pending under the existing checkpoint owner. Recovery requires confirmed current health and the existing native readback and revision checks. A genuine selector mismatch still uses the established guarded recovery path. Automatic health probes share a bounded in-process cache and backoff in the existing backend. Observations are fenced by target, revision and runtime incarnation, and post-switch proof uses a fresh check. Separate backend processes do not share in-flight cache entries. A pending generation can remain unresolved until fresh health and native evidence are available. The incident's original failed predicate remains unknown. Live continuity, runtime restart comparison for structural changes, and recovery side-effect reduction remain B2–B4 gates.

## B2 — Minimal Runtime Apply

**Depends on:** B1 diagnostics and fixtures for the ordinary and staged apply paths.

- Reuse effective configuration comparison across apply paths, preserving parser safety, candidate integrity, native verification and valid stage ordering. Distinguish selector-only changes, equivalent serialization, and structural changes. Use targeted runtime updates only after verifying support and readback.

**Acceptance:** Byte-identical and semantically equal inputs across both paths cause zero Mihomo recreations and zero Xray restarts. A supported selector-only change uses readback with unchanged process/container identities. Structural changes still validate and activate. Fixtures count runtime calls per affected component, verify that unrelated components receive zero lifecycle calls and prove any suppressed transition is safe.

**Runtime API boundary:** Mihomo's selector API already provides a targeted write and readback for a target in the loaded group. In pinned Mihomo v1.19.31, configuration reload suspends the tunnel and updates proxies, rules, DNS, listeners and TUN. HTTP success alone gives no continuity guarantee. Retain controlled activation for proxy/group or structural changes unless a narrower operation has verified runtime and recovery behavior. Xray exposes handler and routing mutations, but FWRouter generation currently proves the mounted configuration and native client/binding state. Keep the controlled restart for genuine Xray config changes until a targeted update can preserve those proofs. [Mihomo v1.19.31 apply implementation](https://github.com/MetaCubeX/mihomo/blob/v1.19.31/hub/executor/executor.go#L78-L116) · [Xray API](https://xtls.github.io/en/config/api.html)

**B2 status: source implemented.** The comparison preserves sequence order, scalar spelling and types, explicit defaults, identifiers and references. Ambiguous or unavailable comparisons keep the controlled apply path. Format-only reuse retains the verified active representation for native validation and preserves exact SHA bindings through publication and recovery. Xray verifies the mounted config and running incarnation before proceeding after a restart or accepting staged NOOP. Local alias updates already use the metadata path. Generated binding metadata still follows the existing config and native proof contract.

**Remaining gates:** A completed transition is recorded before a missing incarnation can block the operation. Recovery remains gated on the runtime ownership evidence. Unproven recovery paths retain the existing lifecycle actions. B3 owns broader rollback side-effect reduction. Qualified child-process and native Xray Docker profiles require their designated isolated runners. LAN continuity and live acceptance remain B4 work.

### B2.1 — Mihomo and Xray version audit

**Status: audit and incumbent Xray pin complete. Candidate upgrades deferred pending native image qualification.** Inventory captured on 2026-11-20 on an amd64 host. Local image IDs matched the amd64 config descriptors of the incumbent registry indexes.

| Component | Running binary and image ID | Selected source reference and incumbent index digest |
| --- | --- | --- |
| Mihomo | v1.19.31 · `sha256:ab5d4cf7e192b941f7a238bdb2450d7437d3499b51da1c13669945ba1a138529` | `metacubex/mihomo:v1.19.31` · `sha256:739edd73a352d6beb82fad6790ef9d417a4d6f061584cc3cab1bd4536d1c60e5` |
| Xray | 26.2.6 · `sha256:60c138250e2dca6e54259a1333188692fdf2560cb2dae7f6610b3ee92f6c3ff1` | `teddysun/xray:26.2.6@sha256:8c2017c7af290fe3c15268e82e658f3d51dab4369bac164b79cf2b2f175cdcea` |

The installed and source Compose files matched before the audit. The Xray Compose reference previously floated at `latest`. This change locks it to the exact running multiarch index, preserving the current amd64 image and the index's ARM64 selection. The existing isolated native test binary already pins Xray 26.2.6. Mihomo's version tag remains consistent across Compose, subscription validation, maintenance evidence producer and consumer, and acceptance provisioning. The maintenance checkpoint binds that literal image reference, so an immutable Mihomo pin would require coordinated admission and rollback qualification. Historical snapshots and fixtures retain their original pins.

- **Mihomo candidate v1.19.34.** Registry index `sha256:4c2495d705f152897a58eac410350f243835fd01ce2fe44949a571faa824d532`, ARM64 manifest `sha256:5538188a7704b338bee21849cfe8df3a382a2636a5872139c32157cc926b07bf`. v1.19.32 changes the default TUN stack to mips. v1.19.33 adds TUN DNS mode and fixes TLS security and fake-IP reload state. v1.19.34 modifies TUN auto-routing and still suspends the tunnel during full config apply. Keep the controlled B2 lifecycle. [v1.19.32](https://github.com/MetaCubeX/mihomo/releases/tag/v1.19.32) · [v1.19.33](https://github.com/MetaCubeX/mihomo/releases/tag/v1.19.33) · [v1.19.34](https://github.com/MetaCubeX/mihomo/releases/tag/v1.19.34) · [Pinned apply implementation](https://github.com/MetaCubeX/mihomo/blob/v1.19.34/hub/executor/executor.go#L78-L116)
- **Xray candidate v26.11.9.** Same-vendor Docker Hub index `sha256:b3e117b834878d1e12f91e7b16270db104c2f0e5f0effbc0ece9088c473b5393`, ARM64 manifest `sha256:ab90b4da4c9bb55b8d82a0ca35b337e40c2618e5a96c66515c076b667c3849aa`. The same-vendor amd64 OCI config preserves the command, user, working directory and declared volumes. The official XTLS image is also multiarch and has its own entrypoint and volume contract. Both upstream binaries warn that WebSocket is deprecated. FWRouter uses it for generated VLESS subscriptions. Its Freedom egress blocked the private loopback target during the isolated VLESS round-trip. Validate explicit DIRECT policy and private SOCKS handoffs before changing the image. v26.10.31 disables MPTCP by default for security. The retained version predates that release and host MPTCP is enabled. Applicability of the upstream kernel issue to the running host remains unverified. [v26.11.9](https://github.com/XTLS/Xray-core/releases/tag/v26.11.9) · [v26.10.31](https://github.com/XTLS/Xray-core/releases/tag/v26.10.31) · [Freedom source](https://github.com/XTLS/Xray-core/blob/v26.11.9/proxy/freedom/freedom.go)

**Validation:** Registry metadata confirmed amd64 and ARM64 descriptors for both incumbents and candidates. Checksum-verified upstream amd64 binaries validated synthetic configurations and started in a private loopback-only namespace. Mihomo selector write/readback, proxying and DNS passed. One idle loopback TCP connection survived reload. Xray returned a loaded VLESS user via HandlerService. The same VLESS loopback round-trip passed with the checksum-verified upstream 26.2.6 binary. In 26.11.9 it reached the server and Freedom blocked the destination under its default policy. These smoke results do not qualify the candidate OCI images. B1/B2 backend regression passed 275 tests on the retained source pins. Full generated-config startup and B1/B2 transitions on candidate images, transparent TUN/routing, complete VLESS egress and Reality coverage, ARM64 execution, image rollback, and LAN continuity remain **NOT_VERIFIED**. Stable containers and deployment evidence were untouched.

## B3 — Recovery & Concurrency

**Depends on:** B1 outcome diagnostics, B2 affected-component decisions and existing checkpoint and D2 recovery contracts.

- Preserve last-known-good state through partial application. Restore only affected components. Verify installed artifacts, runtime identity, effective selectors and required native evidence before clearing recovery state. Reject stale writers using existing ownership, revision and incarnation fences.

**Acceptance:** Inject a failure before and after each physical apply step. Verify zero lifecycle calls to unaffected components, exact recovery readback for affected components, idempotent resume after interruption, and retained recovery evidence when verification fails. Interleave a refresh, watchdog selection and newer user intent. Stale apply and rollback must make zero writes over the newer owner.

## B4 — Integration QA

**Depends on:** B1–B3 passing targeted regressions and an isolated environment with private networking, fixtures, services and data.

- Exercise scheduled and manual refresh, VPN Auto, partial application, rollback, interrupted recovery and repeat invocation. Measure persistent and new TCP connections, UDP delivery and DNS resolution for direct and VPN traffic. Correlate loss, session resets and outage duration with instance changes.

**Acceptance:** Repeat each scenario against a captured idle baseline and report test counts, session resets, packet loss, DNS failures, outage duration, runtime actions and recovery readback. Semantic NOOP must cause zero runtime lifecycle calls, zero attributable session resets and zero attributable UDP/DNS failures. For targeted and structural changes and rollback, agree quantitative continuity budgets before execution. Missing samples or unverified recovery leave the gate open. Disruptive checks run only in isolation. Independent QA reviews results and explicit remaining gaps.

## Boundaries

Optional-module architecture belongs to **Stage 6.4 Core/Services**. This work stays within existing runtime ownership. Scope excludes a broad runtime rewrite, a second global orchestrator, and parallel deployment or reconciliation systems. Preserve D2 qualification, lifecycle, identity, rollback and recovery contracts. Documentation work involves no Stable changes.
