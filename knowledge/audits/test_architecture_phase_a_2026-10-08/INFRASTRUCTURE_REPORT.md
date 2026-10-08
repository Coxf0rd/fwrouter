# Phase A — Test Infrastructure Audit

Date: 2026-10-08
Audited source: eaecab548a9e05579fd9c27396723a155224f175 (stage/test-architecture-ci-stabilization)
Scope: static inspection of the non-backend test system, gate planner/runner, UI and installer/HA suites, native-runtime gate contracts, workflows, manifest-owned fixture metadata, and tracked audit/staging harnesses. No application source or tests were changed; no tests, imports, harnesses, installer, native runtime, or production commands were executed. GitHub run metadata and retained artifacts were read-only.

The per-file inventory is in [INFRASTRUCTURE_MATRIX.csv](INFRASTRUCTURE_MATRIX.csv). It covers 25 UI files, installer and Home Assistant tests, gate/smoke contract tests, gate/smoke implementation, workflows, native test assets, and tracked audit harnesses. The manifest contains 150 test files total at this HEAD; backend test-by-test details belong in the companion backend audit. UI is a static site: there is no ui/package.json, browser-test package, or checked-in Playwright/Jest/Vitest configuration. The 25 UI files run as standalone Node scripts under Node's built-in test-file runner.

## Executive summary

The checkout has a useful fail-closed, serial test coordinator: it validates the file catalog, computes changed-path plans, invokes selected suites with time and output bounds, drops inherited environment variables, hashes plan/report metadata, and refuses live/destructive pytest markers without staging qualification. Workflows use read-only GitHub permissions, pinned action SHAs and Python/Node versions, no production secrets, and a temporary-target installer/API smoke separate from affected suites.

The infrastructure is not ready to serve as a complete required CI acceptance system. Static inspection found three material gate issues to resolve before reliance:

1. make_plan expands domain dependencies by one edge only. The declared graph has longer chains, for example watchdog → provider → subscription/protocol/xray/database. Tests in transitive dependencies are not automatically selected. The one-hop behavior is confirmed; whether every edge is intended as a required transitive dependency should be decided and contract-tested in Phase B.
2. Native tests become mandatory for protocol/Mihomo/Xray source paths, while the normal workflow invokes run without --include-native or validator arguments. command_run then blocks mandatory native tests before execution. The L6 workflow includes native selection but does not supply an immutable Mihomo binary or Xray image; it has no provisioning step and explicitly refuses downloads. Native workflow compatibility is an open gate.
3. L0 invokes git diff --check without an immutable base or HEAD range. In a clean Actions checkout it checks working-tree/index changes, not the committed PR diff.

Further gaps: isolated smoke replaces the /api/v2/state/system projection function with a fixture, so it checks the route wrapper and response envelope rather than the actual canonical Core projection. UI tests mostly use VM/fake-DOM behavior and source assertions; they do not execute a browser. The manifest records duration_measured_seconds as null for all 150 rows. The native Xray test validates a synthetic isolated Xray process, not full FWRouter application lifecycle. Full application Xray CRUD, crash/restart, applied-state parity and recovery remain unproven.

## Inventory and classification

Manifest version 2026.10.07.1 lists 150 files, one primary level/domain per file, runtime, fixture owner, isolation, network/native/slow/destructive attributes, and per-file limits. Its source_head field still says 489d0e4 while current HEAD is eaecab5. It has eight node overrides overall, none for UI. The 25 UI files are all L1 or L2, non-native, no-network and non-destructive, with 30-second per-file limits. All duration measurement fields are null.

UI suites cover Admin/User presentation, provider member and exclusive-source controls, health/events/journal, external client forms, manual checks, request loading/lazy reads, subscription refresh/delete, shared mutation UX, and localization. Many evaluate production JavaScript using vm with hand-built window/document objects or inspect source using regular expressions. This gives deterministic coverage for helpers and code contracts, but cannot verify real browser layout, CSS cascade, focus/keyboard behavior, event propagation, navigation, network waterfall, or deployed API behavior. Some tests use fake-fetch/deferred-promise interactions; most are a single file-level Node suite rather than named node:test cases.

Home Assistant tests combine YAML structure checks with mocked API responses and verify state/provenance/readback semantics. installer/test-install.sh runs real installer shell against mktemp targets with host dependency installation and unit enabling disabled; it checks exported component boundaries and dependency dry-run lists. It does not exercise real systemd or live Docker. There is no trap to clean all mktemp roots on direct invocation; under the gate runner, TMPDIR is coordinator-owned and expected to be removed at the end.

Gate contract tests exercise manifest shape, unknown-path rejection, domain selection size, a native-required example, L7 non-execution, subset non-promotion, baseline classification, output caps/process-group termination, environment scrubbing, and staging-marker denial. Smoke contract tests exercise temporary DB/read-only checks, fake HTTP/installer adapters, native hash/parity rejection, loopback-only live profile checks, output caps and timeouts. These are meaningful helper-contract tests, not evidence that native or live profiles actually ran.

See the matrix for each file's observed contract, level, dependencies, isolation, and hosted compatibility.

## Planner, workflows and acceptance evidence

The planner rejects unknown paths, invalid/stale plan digests, source/base drift, manifest mismatch, unclassified test files, forbidden L7 selection through normal subset mode, and selected baseline nodes that fail or skip. Reports and output are bounded. Suite execution is serial, starts a separate POSIX process group, and enforces deadlines/output caps.

Findings from source:

- **Dependency closure — confirmed one-hop expansion, contract decision required.** gate.py lines 209–215 adds dependencies only for original domains. manifest.json lines 3–83 contains multi-edge chains. Add graph-closure tests with cycle handling and critical examples in Phase B.
- **Native admission — confirmed workflow/planner mismatch.** manifest path rules require native suites for protocol/Mihomo/Xray changes (manifest lines 137–143 and 302–377). test-gates.yml lines 59–69 calls run without native opt-in or validators. gate.py lines 952–990 blocks a required native suite when include_native is false and otherwise requires explicit Mihomo binary or immutable Xray image. test-full-suite.yml lines 33–45 requests native inclusion but supplies neither asset. No workflow provisions them. This is an unclosed design gate, not a failure observed in audited runs.
- **L0 whitespace scope — confirmed.** gate.py lines 900–930 invokes git diff --check without a revision range. Syntax checks use changed paths; whitespace validation does not.
- **Default levels — metadata mismatch.** manifest policy says L1/L2, but make_plan adds primary levels of every selected file and command_run executes each selected file at a resulting level (gate.py lines 221–231 and 952–953). An affected plan can therefore include L3/L4. This is a safe superset but costs more than the manifest policy describes.
- **L5 trigger scope — possible gap.** L5 anchors are added only when original changed domains intersect shared_domains (gate.py lines 225–231). core is a path domain but absent from the listed shared-domain set. Confirm whether Core source paths must trigger shared-contract anchors.
- **Smoke projection — confirmed scope limitation.** Isolated smoke runs real TestClient Health/system routes with lifecycle disabled, but patches state_routes.build_system_state_projection (smoke.py lines 151–200). It checks only that state is a dictionary. It does not validate canonical Core state composition. Health uses a real route over temporary DB. Native validation is explicitly not_requested for this profile (smoke.py lines 305–339).

Current workflows:
- test-gates.yml runs on PR, main push and manual dispatch, not feature-branch pushes. It uses ubuntu-24.04, Python 3.11.2, Node 22.22.2, hash-locked dependencies, L7 dry-run, isolated L4 smoke and affected suites. Permissions are contents: read, checkout credentials are disabled, and there is no production secret or deploy step.
- test-full-suite.yml is weekly/manual serial L6 with 120-minute cap. It does not install native runtimes and cannot currently supply the explicit binary/image arguments the runner requires.

Read-only GitHub evidence:
- Run 37705115129 for eaecab5 and PR run 37705002330 for a5b403c completed successfully.
- The retained eaecab5 artifact shows a docs-only diff: selected_files was empty, only L0 was recorded, eligible=true, and L0 took 0.035 seconds. The workflow separately completed L7 dry-run and isolated smoke, but the retained artifact has only plan/report JSON, not a smoke receipt.
- These runs prove checkout/setup/dependency install, catalog validation, L7 dry-run, isolated smoke step completion and docs-only promotion. They do not prove representative affected suite selection, native admission, source-path gates, or CI completeness.
- Prior main run 37698048850 on e49510c failed at Plan and run default affected gates; setup/catalog/L7 and isolated smoke succeeded. Exact logs/artifact were unavailable through read-only API. The failure remains unclassified. Later docs-only successful runs do not establish it was fixed.

GitHub runner limits were checked by the lead auditor against current official docs: public standard Ubuntu 24.04 is listed as 4 CPU, 16 GB RAM and 14 GB SSD; free-plan concurrency and artifact/cache quotas are finite. Recheck volatile limits during CI implementation. Measure actual disk after checkout/dependencies, Docker image size/availability, CPU/RAM, duration and artifact sizes. Docker installed on a hosted runner does not establish guest systemd/PID 1, cgroup delegation, nftables routing or reliable boot/fault recovery. References: [GitHub-hosted runners](https://docs.github.com/en/actions/reference/runners/github-hosted-runners), [Actions usage limits](https://docs.github.com/en/actions/reference/limits).

## Quality, safety and resources

Positive practices:
- Per-suite timeout/output/artifact caps; serial execution; bounded JSON evidence.
- Pytest configuration disables coordinator cache use and selects dedicated basetemp. conftest intends to disable dotenv loading, sets temporary state, disables schedulers, waits for the default job manager, clears settings/probe cache, and replaces default Xray/Mihomo/dataplane adapters (conftest.py lines 18–22, 90–132, 194–215). There is an import-order caveat: conftest imports `NoopXrayAdapter` from `fwrouter_api.adapters.xray` before line 24 sets `Settings.model_config["env_file"] = None`; that facade imports `xray_real`, whose module-level `DEFAULT_XRAY_ADAPTER = RealXrayAdapter()` calls `_default_xray_config_path()`, which calls `get_settings()` (xray_real.py:1404; xray_common.py:151–152). Thus the first settings instance can read its configured `/opt/fwrouter-api/.env` before the test harness disables dotenv. Static inspection proves this ordering and lookup path; whether that file exists/is readable in a particular runner, and whether the process environment is otherwise scrubbed early enough, is environment-dependent. Treat this as a confirmed isolation defect in the harness contract, not evidence that secrets were observed or that an audited run accessed production.
- Gate builds a clean subprocess environment, disables schedulers, drops provider keys/proxy variables, and omits raw test output from reports.
- Native Xray fixture uses immutable local image, --pull=never, --network none, unique container, non-root user, cap drop, no-new-privileges, CPU/memory/PID limits, and exact loaded identity/config-hash readback (test_xray_native_readback.py lines 20–76 and 103–139). It is meaningful component-native coverage when provisioned.

Limits:
- conftest's subprocess guard wraps subprocess.run and matches selected Docker/systemctl patterns, not Popen, arbitrary shell, sockets/HTTP, general absolute-path reads or Docker socket use (conftest.py lines 113–132). Temporary settings/default-adapter replacements reduce risk but are not an OS sandbox. A test directly instantiating a real adapter needs proof its Settings/writer/runtime paths are isolated before the first call.
- The default-adapter import-order issue above means temporary settings replacement occurs too late to establish a strict “tests cannot read the deployed `.env`” guarantee. Phase B should set an isolated env-file policy before any application imports (or remove the eager real-adapter construction) and add a regression contract that observes the first Settings construction without importing production secrets. A fresh runner usually lacks `/opt/fwrouter-api/.env`, but absence on hosted runners does not make the isolation boundary correct on developer or self-hosted machines.
- A writer-lock interaction was recorded in the separate unfinished operational branch at 6004400, not in this audited catalog. Its STAGING_GATES.md lines 19–24 says the early harness called Xray create before overriding Settings and touched /run/fwrouter-v2/xray-writer.lock; it did not mutate DB/config/container/intent. The corrected run asserted its temporary lock path. This is a demonstrated fixture-construction hazard, not evidence a current-main test touched production.
- test_protocol_native_validation.py tests fixture normalization → candidate generation → pinned mihomo -t, including negative transport/security cases. It makes no network connection and needs an already available binary.
- Xray native readiness uses bounded 0.2-second polling after container start/restart. This is test readiness polling, not a production sleep, but needs hosted repeatability measurement. vpn-auto-exclusive UI has one zero-delay timer to advance an event-loop turn.

### Additional operational scripts and privileged runtime support

The non-manifest operational assets were checked separately because their names can look like acceptance tests even though some target a live host. The full per-file/script catalog is in the matrix.

- **Critical live-mutation hazard:** `backend/scripts/linux-live-acceptance.sh` is not safe as a generic acceptance command. It defaults to the live loopback API and unconditionally POSTs global Direct, enables/disables Core bypass, collects traffic with `dry_run:false`, and POSTs global VPN (`backend/scripts/linux-live-acceptance.sh:78–91, 115–125`). Optional LAN/Tailscale overrides are only cleared near normal script completion (`:93–104, 133–140`); there is no `trap`/finally restoration of the original global mode or bypass if any intermediate request fails. Requests are printed but response JSON is not validated as a pass condition. Classify as live-mutating, manual-operator-only, staging-required; never call it from routine CI. This is a source-level risk, not a claim that it was executed during this audit.
- `backend/scripts/check_boot_persistence.sh` is a diagnostic collector rather than an acceptance gate: every command is wrapped in `|| true`, so missing units/routing/listeners/errors cannot fail the script (`:4–9`). Its output must not be represented as boot-persistence PASS.
- Other scripts with live defaults are distinct from tests: `post-code-reconcile.sh` restarts the API and performs bootstrap; `post-clean-reset-bootstrap.sh` performs subject sync/discovery; `post-reset-rules-full-update.sh` mutates rules; `post-reset-subscription-refresh.sh` saves a caller-provided URL and refreshes; `collect-server-diagnostics.sh` copies DB/config and sends a watchdog read/check request. These should remain outside automatic CI and any bundle handling should use redaction/retention policy. `export-clean-tree.sh` removes its caller-supplied target before export (`:5–13`), so tests must pass an owned temporary path.
- `installer/test-install.sh` is a real install/export test against temporary targets, but creates multiple `mktemp` directories/files without a cleanup trap; repeated CI runs can accumulate disk usage. The script avoids host mutation by setting host-dependency, Python setup and unit enabling off. Its dependency dry-run may still inspect host package/Docker availability, so results are environment-sensitive.
- Host dataplane helpers are privileged runtime machinery, not ordinary test assets: apply/rollback mutate nftables and policy routing; apply may delete the owned table and flush conntrack; boot preflight creates files, writes the routing-table entry and applies `sysctl --system`. They require a disposable network namespace/VM plus explicit command capability fencing before any tests exercise them. The read-only checker also needs native host tools and currently reports successful candidate validation separately from actual dataplane enforcement.
- `docker-subject-events.sh` is an infinite Docker event loop with 2-second debounce and fixed 5-second reconnect delay; it triggers the live subject-sync API and suppresses individual sync failures. Treat resource/timing characterization as operational evidence, not a test. Traffic collector helpers read secret-bearing generated config, query localhost Mihomo/Xray and Docker, and POST non-dry-run collection through a systemd timer.
- The checked-in Compose runtime definitions do not declare CPU, memory, or PID limits. Mihomo uses host networking, `NET_ADMIN`/`NET_RAW`, and `/dev/net/tun`; Xray uses an external Docker network and a mutable `latest` image tag. Native test containers have stronger explicit isolation/limits than the production Compose definitions, but hosted CI still needs immutable image provisioning and measured runner capacity before native gates become required.
- No per-suite duration/CPU/RAM/disk trend exists. Native container limits do not constrain Python test process. The 30-second default file limit has not been calibrated from stored runtime data.
- No browser test is in the manifest. VM/fake-DOM tests are not browser/render acceptance.

## Coverage gaps that block later acceptance

- **Full application Xray CRUD/recovery:** native component test covers synthetic Xray identity and restart, not FWRouter API→Core→DB→generation→native apply/readback/persistence. No full create/edit/fixed-route/enable/disable/delete parity and crash-boundary recovery acceptance was found. This remains insufficient for Operational Performance Fixes acceptance.
- **Crash/fencing:** isolated revision/checkpoint tests exist, but no qualified full-stack L7 systemd/cgroup/API crash matrix. L7 is a non-executing BLOCKED_NO_DISPOSABLE_STAGING_ATTESTATION result by design.
- **Mihomo/actual routing:** candidate generation and native validation exist; application-generated routing/traffic is not proven in a disposable system boundary.
- **Provider safety:** negative-call tests exist, but zero-provider normal-path behavior should be asserted at shared integration boundaries; no credentials belong in ordinary CI.
- **UI:** no real browser smoke verifies request waterfall, console errors, layout/overflow, locale/navigation or mutation-free render.
- **Coverage metadata:** file-level tags do not trace individual assertions to contracts/criticality or distinguish independent evidence from white-box/component/integration/native layers.

## Baseline and historical failures

No tests were run in this audit. Do not reclassify backend baseline failures based on this report. Repository baseline status contains historical ux-presentation failure evidence and reports a later test-only fixture correction, but that is not a fresh run here. The failed main Actions run is also unclassified until its exact failure is recovered or reproduced. Do not use a blanket allowlist.

## Audit harness access classes

Tracked audit scripts are research/measurement tools, not test suites; none should be automatically selected by normal CI. The matrix lists each script.

- **Live mutating/deploy:** live_operational_dataplane scripts backend_deploy_api_restart_once.py (backend deploy + fwrouter-api.service restart), xray_create_once.py, xray_alias_edit_once.py, xray_delete_once.py and xray_fixed_route_once.py (live API mutations). Explicit operator authorization and dedicated test client are required; exclude from CI.
- **Read-only live/proc/network:** measure_attribution.py, measure_dataplane.py, measure_restart_overlap.py, measure_xray_client.py, read_only_control_plane.py, live_snapshot.py, sample_live_io.py, compare_technical_logs.py, profile_system_summary_readonly.py. They inspect host process/cgroup/systemd/Docker or production DB/config via read-only paths; some issue bounded outbound HTTP/ping and assume host topology, so they are not hosted tests.
- **Isolated/local native containers:** current_native_validation.py and native_validation_sandbox.py create/remove unique Docker containers using preexisting images, no network, read-only inputs and resource caps. They need Docker and native asset provisioning; they do not test full systemd recovery.
- **Isolated SQLite/source replays:** job_manager_cleanup_bench.py, settings_noop_bench.py, summary_logging_probe.py, run_summary_logging_probe.py, control_plane_bench.py, xray_lifecycle_probe.py, isolated_startup_foundation.py, resource_sampler.py, retention_microbenchmark.py, read_retention_benchmark.py, attribute_servers_isolated.py, attribute_inventory.py, scoped_egress_binding_file_ab.py, replay_revision_wal.py use synthetic/temp state or copied DB. Keep explicit invocation and source/input assertions.
- **Sensitive host reads:** live_snapshot.py, profile_system_summary_readonly.py and compare_technical_logs.py refer to production DB/log/runtime paths. Safety depends on read-only path construction/permissions, not filenames.
- **Browser audits:** ui_predeploy_reference.mjs, ui_performance_measure.mjs and ui_settings_complement.mjs use existing Chromium/CDP and live pages. They are measurement harnesses requiring authorized browser session; do not trigger them in CI.

No test, installer or audit script was executed here. Classifications above come from source.

## Prioritized Phase B–E plan

### Phase B — Test Refactoring & Standardization
1. Test L0 diff scope against committed range in clean checkout.
2. Decide and contract-test dependency closure/cycles and whether Core paths trigger L5 anchors; align level policy with actual L1–L4 selection.
3. Design native profiles with pinned assets available on hosted runners or retain an explicit blocking gate; never silently skip required native tests.
4. Split UI source-regex assertions from helper-behavior assertions; add case-level IDs and unify only proven shared fake DOM/response fixtures.
5. Version/own fixtures, assert state/lock/runtime paths before native calls, and add direct-run temp cleanup to installer tests.
6. Recover/reproduce failed e49510c Actions run and classify exact IDs.

Acceptance: transitive dependencies/native gates have contract tests; source-range L0 works from clean checkout; no unclassified/novel failures; isolated native calls cannot reach production paths; UI failures map to actionable cases.

### Phase C — Missing Test Coverage
1. Add full synthetic FWRouter Xray application lifecycle: CRUD, fixed route, enable/disable, exact API/persistence readback, generated/mounted/native identities, last-good and bindings parity.
2. Crash at apply/readback/persist boundaries; restart API/Xray runner; test idempotency, CAS/revision/incarnation conflicts and rollback.
3. Exercise Core/Provider/Mihomo invariants and negative calls without credentials or production routes.
4. Add real-browser smoke for Admin inventory, Provider Settings and Xray client flow; check RU/EN, console errors, layout and request policy.

Acceptance: convergence and failure/no-false-success are proven in documented disposable isolation; all owned processes/containers/cgroups/files are cleaned.

### Phase D — GitHub Actions CI
1. Push L0–L1; PR dependency-complete affected L0–L5; main integration/native smoke; separate manual/nightly/release L6; release L7 only after qualification.
2. Separate lightweight tests from native/container suites; decide how to provide pinned assets within policy.
3. Retain bounded evidence for every gate, including L4, with per-suite IDs and times; no production secrets/live URLs.
4. Bound disk/cache/image growth and measure CPU/RAM/time/artifacts; avoid duplicate builds.

Acceptance: representative PRs execute expected tests and retain evidence; shared/transitive dependencies are selected; missing required suite blocks clearly.

### Phase E — CI Acceptance
1. Run real GH cases for provider, protocol, Core, UI, installer and unknown/shared paths, plus baseline cases.
2. Verify isolation, env scrubbing, process kill, caps, cache/disk limits and no production access.
3. Exercise native admission with pinned assets and compare generated/native loaded state; do not infer L7 from Docker isolation.
4. Repeat only flakiness-risk cohorts and report exact counts/times/resources; register hosted-incompatible systemd/network/fault cases.

Acceptance: stable required gates with exact evidence, no unclassified baseline failure, native boundaries proven, and L7 either qualified in disposable hosted isolation or explicitly blocked.

## Readiness criteria

The test system is ready for Phase B/CI rollout when inventory and contract traceability are complete; planner closure and levels are tested; L0 checks committed source; native gates have an executable hosted path or block explicitly; baseline defects have exact-node evidence/ownership; full application Xray recovery proves applied/native state and last-good behavior; browser claims have browser evidence; ordinary CI cannot access production secrets/paths; and representative GH runs retain gate evidence.

This report is static except cited GitHub run summaries/artifacts. It does not claim local test pass, timing/resource measurements, native binaries available here, GH-hosted systemd compatibility, or root cause for failed run 37698048850. Phase B and CI acceptance remain open.
