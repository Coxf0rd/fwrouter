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
