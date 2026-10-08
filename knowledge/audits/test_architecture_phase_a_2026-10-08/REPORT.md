# Phase A — Comprehensive Test Audit

Date: 2026-10-08. Audited source: **`eaecab548a9e05579fd9c27396723a155224f175`**; branch: `stage/test-architecture-ci-stabilization`.

**Disposition: Phase A audit delivered for operator review. Phase B–E are not started. The test system is not accepted for full application release validation.** Operational Performance Fixes remain PAUSED/BLOCKED. This audit changes documentation/evidence only.

## 1. Executive summary

The inventory covers all **150 manifest suites**: 120 backend modules, 25 UI scripts, two installer-path suites (one is an HA action contract), two gate/smoke contract suites and one integrations Home Assistant suite. The backend contains **1,372 static test definitions**, 35 parametrized definitions and 6,624 direct Python assertions. These are source counts, **not collected/executed pytest nodes**; parameter expansion was not evaluated. There are also 36 fixture payload/metadata files, 742 backend fixture/helper/test-double definitions, and 36 tracked historical audit harness scripts. The complete source-asset ledger includes 278 files and their SHA-256 hashes; production operational dependencies are distinguished from ordinary test suites.

Useful coverage already exists: selector/recovery interleavings and CAS, per-source provider credentials and request budgets, protocol normalization and native candidate validation, generation checkpoint recovery, persistence/migrations, public projections and negative no-call assertions. These contracts should be retained, not re-created or weakened to get PASS.

The principal blockers are:

1. **Pre-fixture isolation is incomplete.** An import-time default Xray adapter can create Settings before the harness disables production dotenv lookup. The subprocess blocklist is not an OS sandbox. This is source-backed reachability, not an observed secret read or production mutation in Phase A.
2. **Full application Xray CRUD/crash/recovery acceptance is missing.** API tests with fake adapters and a separate native Xray component test do not jointly prove API/job → Core → DB → generation → native runtime → readback → persistence convergence.
3. **Native CI admission is incomplete.** Source changes can require native suites, but existing workflows do not supply their explicit admission/provisioning inputs. Ordinary direct invocation can skip the native Xray test when dependencies are absent.
4. **Affected selection and L0 have source-level coverage limits.** Dependency expansion is one-hop; whitespace checks do not inspect an explicit committed range. Neither should be assumed to prove the new CI contract.
5. **Baseline evidence is historical, not a current green suite.** There are 52 historical backend IDs: 41 have later selected PASS evidence, 11 have a last retained failure. No blanket exception is approved.

No tests, test collection, application imports, native binaries, Docker operations, production DB/API, deployment, restart or provider requests were performed. Only source/JSON/AST analysis and read-only Git/GitHub evidence queries were used. Unknown timing, behavior, coverage and host compatibility remain explicitly unmeasured.

## 2. Methodology and complete inventory

### Source and evidence boundaries

- Git/PR state was checked before work; the working tree was clean at entry. Current source is merged main `eaecab5`, not the retained operational branch `6004400`.
- Enumerated tracked files, joined all manifest rows, parsed Python test bodies/fixtures/helpers without imports, and reviewed actual assertions, called symbols and external boundaries. Matrix expression excerpts are source evidence, not execution results or a claim that every oracle is sufficient.
- Reviewed gate/planner/plugin/smoke/workflows, UI VM/source tests, installer/HA checks, fixture documentation, native fixtures, operational scripts and historical harnesses.
- Traced critical production implementation paths only by reading source. No config loader or test harness was invoked.
- Inspected existing GitHub job metadata; no workflow was triggered or re-run.
- Consulted official GitHub runner/limits documentation for future platform constraints; no infrastructure was provisioned.

### Inventory files

| Artifact | Coverage and use |
|---|---|
| [TEST_INVENTORY.csv](TEST_INVENTORY.csv) / [JSON](TEST_INVENTORY.json) | One consolidated row for every one of the 150 manifest suites: actual targets/contracts, level/domain, assertions, mock/isolation limits, dependencies, criticality and hosted viability. |
| [BACKEND_TEST_MATRIX.csv](BACKEND_TEST_MATRIX.csv) / [JSON](BACKEND_TEST_MATRIX.json) | All 1,372 static backend definitions, source anchors, fixtures/decorators, direct assertion expressions/lines, expected exceptions, mock calls and quality signals. No runtime node expansion. |
| [BACKEND_FILE_MATRIX.csv](BACKEND_FILE_MATRIX.csv) / [JSON](BACKEND_FILE_MATRIX.json) | All 120 backend files, actual referenced targets/assertions, positive/negative signals, isolation and hosted requirements. |
| [INFRASTRUCTURE_MATRIX.csv](INFRASTRUCTURE_MATRIX.csv) | 121 infrastructure/dependency records, including every UI suite, gate/installer/HA suites, native fixtures, workflows, operational checks and historical harnesses. Some paths have multiple named facets (for example installer execution versus cleanup); grouped/facet records are not unique file or additional test counts. |
| [HELPER_MATRIX.csv](HELPER_MATRIX.csv) | 742 backend helper/mock/fixture definitions with actual called symbols/assertions and source anchors. Nested doubles are counted separately from test definitions. UI helper scope is covered in the infrastructure matrix. |
| [FIXTURE_MATRIX.csv](FIXTURE_MATRIX.csv) | All 36 fixture payload/metadata files, owner, format, capture provenance, syntax boundary and literal consumer candidates. Literal matching does not prove dynamic use or non-use. |
| [ASSET_CATALOG.csv](ASSET_CATALOG.csv) / [JSON](ASSET_CATALOG.json) | 278 tracked source assets, role, size and digest. Covers both test infrastructure and reviewed operational dependencies; does not silently enroll historical/live helpers in CI. |
| [BASELINE_FAILURES.csv](BASELINE_FAILURES.csv) / [JSON](BASELINE_FAILURES.json) | All 52 historical IDs, current source anchor, retained diagnosis/status, owner, uncertainty and proposed triage. |
| [TIMING_EVIDENCE.csv](TIMING_EVIDENCE.csv) | Historical cohort and actual hosted whole-job durations; explicitly separates their commits/environments from current unexecuted suites. |
| [GITHUB_RUN_METADATA.json](GITHUB_RUN_METADATA.json) | Existing job/step status and timestamps, collected read-only. |

Detailed reviews: [backend report](BACKEND_REPORT.md), [infrastructure report](INFRASTRUCTURE_REPORT.md).

### Taxonomy at the audited source

| Level | Current file defaults | Actual interpretation |
|---|---:|---|
| L0 | Independent gate | Static source/syntax/basic checks, not a primary test-file classification. |
| L1 | 98 | Unit/white-box; includes most Node VM/source tests. |
| L2 | 25 | Component/public contracts; mocks must be explicit. |
| L3 | 25 | Multi-component integration defaults; mocked runtime means these do not automatically prove native integration. |
| L4 | 2 | Narrow smoke/native-readback file defaults; isolated smoke is also a separate command. |
| L5 | Aggregation policy | Reviewed cross-domain anchors; no duplicated primary file level. |
| L6 | Full-manifest policy | Scheduled/manual/release/major checkpoint only; not run here. |
| L7 | Non-executing deny gate | No qualified full-application release harness accepted; dry-run rejection is not a staging PASS. |

Eight node overrides refine mixed file defaults. All 150 manifest `duration_measured_seconds` values are null. There is no checked-in browser suite/configuration in the normal catalog. UI files are standalone Node scripts, often with file-level rather than scenario-level results. No coverage percentage is claimed; line/branch/mutation coverage was not measured.

## 3. Findings: quality and architecture correspondence

Priority refers to a **test-system correction**, not an assertion that production is currently broken. Confirmed static facts, potential execution hazards and unmeasured behavior are separated.

| ID / priority | Finding and evidence | Consequence / next action |
|---|---|---|
| A01 / P0 | `backend/tests/conftest.py:12–24` imports the Xray facade before disabling dotenv. `adapters/xray.py:30` imports `xray_real`; `xray_real.py:1404` constructs the default adapter; constructor `:71` calls `xray_common.py:151–152` → `get_settings()`, whose config has `/opt/fwrouter-api/.env` at `core/config.py:22`. | Confirmed pre-guard Settings construction and possible production dotenv/default-path resolution. Actual read/disclosure is unobserved. Establish test bootstrap before imports and assert all state/config/lock paths before native work. |
| A02 / P0 | `conftest.py:132–157` wraps `subprocess.run` with a finite command blocklist. It does not forbid arbitrary filesystem access, sockets, `Popen`, alternative command spellings or all Docker actions. | Defense-in-depth only, not confinement. Execute risky scenarios only in a proven ephemeral environment without production mounts, authority or credentials. Never infer isolation from a marker alone. |
| A03 / P0 | `backend/scripts/linux-live-acceptance.sh:4` defaults to the local API; it posts Direct/VPN, Core bypass, traffic collection and optional overrides, prints responses and has no restoration trap. | A real live mutator, not routine smoke. Register as manually authorized/disposable acceptance only. Do not run it on minisk or enroll it in ordinary CI. |
| A04 / P1 | `backend/tests/test_apply_pipeline.py::test_debug_db_state` (`:503–511`) initializes/prints DB state and ends with `assert True`. | Confirmed ineffective oracle. Decide the intended contract and replace with meaningful assertions or retire the diagnostic test with evidence in Phase B. |
| A05 / P1 | `tests/gates/gate.py:209–215` expands only original-domain dependency edges; the manifest graph has chains such as watchdog → provider → subscription/protocol/Xray/DB. | Confirmed one-hop behavior. Decide mandatory dependency semantics, implement closure/cycle regression in Phase B, and test representative cross-domain edits before relying on affected gates. This is not evidence of a production defect. |
| A06 / P1 | `gate.py:900–930` runs `git diff --check` without a base/HEAD range. | In a clean checkout it does not check whitespace in committed PR changes. Define immutable diff scope and test it in a clean committed fixture. |
| A07 / P1 | `test-gates.yml:59–69` runs without native admission inputs; source path rules require native suites. L6 requests native selection but provisions neither Mihomo binary nor immutable Xray image. `gate.py:952–990` appropriately blocks missing required assets. | Workflow/admission mismatch, not a reason to remove safety checks. Provide pinned assets and explicit profiles in Phase D. A docs-only green run does not close native gates. |
| A08 / P1 | `test_xray_native_readback.py:20–27` skips without image/Docker on direct invocation; Mihomo test defaults to a local `/tmp` binary and checks its version prefix (`test_protocol_native_validation.py:20–43`). | Coordinator-required native coverage must remain blocking, not silently skipped. Immutable supply/provenance and checksum verification need an explicit hosted contract. Native parsing is not a live handshake. |
| A09 / P1 | Isolated smoke patches `state_routes.build_system_state_projection` (`tests/gates/smoke.py:197`) and checks a dictionary/envelope; Health is a real route over temporary DB. | Useful wrapper smoke, not canonical Core projection acceptance. Add an independently expected read-only Core state case in Phase C. |
| A10 / P1 | Broad Xray API/generation tests replace adapters/runner; the native test drives a synthetic Xray instance separately. | Missing full application Xray CRUD/recovery proof. Do not mark the operational milestone complete from those separate checks. See acceptance matrix below. |
| A11 / P2 | 25 UI suites use VM/fake DOM, source regex or synthetic fetch. | Appropriate helper contracts, but no actual CSS/layout/locale/navigation/browser console/network acceptance. Retain unit tests and add a small real-browser gate instead of relabeling them integration tests. |
| A12 / P2 | Backend source contains 11 direct `time.sleep()` calls; coalescing/debounce cases depend on short fake overlap windows. Writer subprocess strings contain additional deliberate holds. | Some are valid bounded readiness polling or latency fixtures. Others have scheduler-dependent overlap and need barrier/event-based admission evidence. No flakiness frequency was measured. |
| A13 / P2 | 39 `_configure_env` helpers; exact AST bodies repeat in groups of 18 and nine. Six `_client` helpers have identical bodies. There are 672 static `initialize_database()` call sites including fixtures/helpers. | Confirmed duplication and potential repeated setup, not a measured bottleneck or 672 runtime commits. Consolidate only equivalent fixtures after timing; keep empty-schema/migration cases deliberately separate. |
| A14 / P2 | Shared default job manager/cache and fixture cleanup are process-level. Fixture calls `wait_for_idle()` before/after tests but ignores false; the manager default wait is bounded. Direct pytest uses fixed `/tmp` basetemp/cache settings. | Possible worker/order/concurrent-session contamination if the wait expires or direct sessions overlap. Prove teardown completeness and unique ownership, then qualify process-level parallelism. No leak was observed because tests were not executed. |
| A15 / P2 | `check_boot_persistence.sh:4–8` wraps every diagnostic with `|| true`; installer direct tests lack cleanup of all temporary roots. | Boot script is an information collector, not an acceptance PASS gate. Cleanup should remove owned artifacts on success/failure; coordinator cleanup is not proof for direct invocation. |
| A16 / P2 | All suite measured durations are null; coverage/CPU/RAM/I/O trends are absent. Artifact metadata has limits, but Python test CPU/RAM/storage are not comprehensively enforced by those numbers. | Calibrate deadlines/resources with isolated runs; no speculative fixture reuse/parallelism or claim of current resource efficiency. |

Additional boundaries: generic default manifest fixture identity is not a complete content-addressed fixture contract; `source_head` is older catalog provenance, not the current execution commit. Source-level regex assertions protect particular text, not arbitrary equivalent behavior. Exact DB-count comparisons detect inserted/deleted rows but not every update to existing rows; full value snapshots and revision assertions are required where the contract is truly read-only. Conversely, an exact count mismatch must not be “fixed” by seeding away a request-owned write without establishing its owner.

## 4. Critical invariant coverage and gaps

This is a contract matrix, not numerical code coverage. Existing unit/state-machine coverage is valuable; missing end-to-end proof is not equivalent to absence of all tests.

| Invariant | Existing evidence in source | Gap / required acceptance |
|---|---|---|
| Core sole writer for global VPN-auto | `test_vpn_auto_concurrency.py`, `test_vpn_auto_writer_guard.py`, selector/runtime-reconcile tests assert fencing, interleavings and no stale persistence. | Whole application runtime/DB convergence with native readback and external writers refused; OS-process contention and restart cases in qualified environment. |
| Persistent exclusive eligibility vs configured Auto/fixed routes | `test_vpn_auto_exclusive.py`, `test_selector.py`, provider Admin/UI suites. | Preserve ordinary fixed Xray routes across the real application lifecycle; no candidate outside exclusive source after restart/recovery. |
| Provider secrets/binding/cache/API evidence | `test_provider_configuration.py`, `test_provider_recovery.py`, `test_stealthsurf_provider_boundary.py`, provider-managed integration and projection suites. | Add shared-boundary normal read/render/selector zero-call coverage where absent; use synthetic negative HTTP states and never production credentials/PATCH. |
| Universal protocol normalization and reject-before-apply | Protocol adapter/subscription tests, mixed inputs and pinned native Mihomo fixtures. | Explicit native provisioning on hosted runners. Parser/config acceptance remains distinct from upstream handshake; account-specific unverified handshakes stay open. |
| Xray external clients and public exports | `test_xray.py`, `test_client_lifecycle.py`, generation/recovery/lifecycle modules assert synthetic CRUD, jobs, export and binding state. | Full API/job create/edit/route/enable-disable/delete plus native loaded/applied/export parity in one isolated stack. |
| Mihomo routing/intent/apply/readback | Mihomo config/runtime, global mode, rules, scoped-egress and dataplane tests. | Native runtime orchestration and traffic/readback against isolated components; actual privileged host routing requires separately qualified hosted L7 capability. |
| SQLite schema/migrations/persistence | Database, provider migration, transfer and job/state tests using temporary SQLite. | Reopen-after-crash/partial-commit and migration contracts spanning real process restart; constraint/transaction property coverage reviewed per case. |
| Revision/CAS/runtime incarnation | Concurrency, generation recovery, provider re-entry and prepared-candidate rejection tests. | Real process crash at apply/readback/persistence edges and competing newer intent; prove no blind replay or stale rollback. |
| Desired/applied/native consistency | DTO/read-model, loaded-identity and exact-readback fakes; separate native loaded-state test. | Independently inspect the native process state, mounted/generated candidate and DB applied projection before success/publication. |
| Generation checkpoint recovery/last-good | `test_xray_generation_recovery.py` models stale/failed checkpoints, receipts, input/incarnation conflicts and idempotence. | Full application crash/restart at staged transitions with immutable runtime artifacts, rather than only synthetic checkpoint state. |
| Emergency Direct/re-entry/rollback | Provider recovery, watchdog and global mode tests model typed failure and temporary effective override. | Qualified isolated outage/connectivity cases; preserved desired VPN/member intent, unlocked probes, fenced verified re-entry. No production outage required. |
| API/Jobs/event durability | TestClient/component job/event tests, timed bounded job wait and serialization contracts. | Terminal job success distinguished from accepted/running response; process restart durability, teardown/ownership and native convergence. |
| Health/read-only projections | State/reconcile/system/diagnostic tests include no-table-init and DB snapshot assertions. | Resolve existing lazy-watchdog write contradiction without weakening read-only assertions; real Core state smoke rather than a patched envelope. |
| UI/security/negative cases | Secret redaction, unsafe input, policy-disabled no-call, RU/EN rendering and fake-fetch suites. | Browser locale/navigation/overflow/console/request acceptance; pre-import capability denial and mutation-free tests across all entrypoints. |

### Mandatory full-application Xray gate

| Scenario | Required oracle |
|---|---|
| Create, alias/edit, route change, enable/disable where supported, delete | TestClient/HTTP and job result, DB intent, real generated candidate, native loaded UUID+email set, applied binding/mode, public export parity. Alias-only edit still needs the correct application persistence result; do not invent a native alias API. |
| Fixed Global/VPN-auto/ordinary/Direct/private routes | All existing test-owned bindings retain their intended semantics; provider internal members never become global targets. |
| Crash/restart at generation/apply/readback/publication | Last-good persists; checkpoint reaches the correct terminal or unconfirmed state; a newer revision is not overwritten. |
| CAS/incarnation/input conflict and partial rollback | No blind replay/retry; restore only with demonstrated ownership; exact native readback required. |
| Repeated operation/no-op | Idempotent identity/binding state, no unnecessary mutations/reloads, exact output consistency. |
| Accepted API response vs terminal completion | A queued/running acknowledgement is not terminal applied success; assertions wait for the bounded job's actual terminal evidence. |
| Cleanup/isolation failure | Only job-owned containers/networks/volumes/state are removed; no production socket/context/mount/credential and no orphan processes. |

A disposable Compose integration network may be necessary for API/Mihomo/Xray communication; it must be isolated from production and external provider accounts. Native Xray `--network none` component success alone is not full application communication proof. Hosted systemd/cgroup/network-namespace cases need actual qualification. Unsupported required scenarios remain explicit blockers or require a separately approved alternative gate, never a mock-labelled L7 PASS.

## 5. Safety, isolation and cleanup

The previous writer-lock incident is recorded **only as retained branch evidence** at `stage/operational-performance-fixes@6004400`, `knowledge/audits/operational_performance_fixes_2026-10-08/FINAL_REVIEW.md`: an initial harness reached `/run/fwrouter-v2/xray-writer.lock` before temporary Settings initialization; the corrected component run asserted isolated paths. The report states no production config/DB/container/intent mutation. Neither the harness nor its fixes were transferred or executed here.

Safety must precede collection/import, not merely test invocation. Tests with temporary `RealXrayAdapter.config_path` still use the shared writer guard unless Settings/run paths are isolated independently. No production lock contention was exercised in Phase A.

Existing native Xray test uses a unique container, no network, limited CPU/RAM/PIDs, dropped capabilities, unprivileged UID and a temporary read-only config. Those are meaningful controls. It still accesses a Docker daemon; testing against minisk's production daemon is not equivalent to a dedicated ephemeral CI daemon.

Production Compose files are deployment definitions, **not staging fixtures**: Mihomo uses host networking/NET_ADMIN/TUN; resource caps and immutable image provisioning differ from test needs. Never copy these into a PR test and assume production isolation. Privileged dataplane apply/rollback/boot-preflight and default-live reset/reconcile scripts are operational dependencies, not safe routine CI.

Provider fixtures are dated sanitized GET observations with fictional IDs/IPs and redacted connection material. Synthetic protocol fixture values are placeholders. They do not authorize real account requests and do not establish universal remote capabilities. Public runner workflows must never consume production `.env`, key material, DB backups or raw provider responses.

Historical audit scripts are separately classified: live mutation/deploy, live read-only host/proc/network, temporary SQLite/source replay, isolated native container, and authenticated-browser measurements. Their existence is not permission to execute them. Printing JSON or returning zero after swallowed errors does not establish readiness/readback correctness.

## 6. Baseline failures

Authoritative retained input: [2026-10-04 baseline status](../test_architecture_cicd_2026-10-04/BASELINE_STATUS.json). The full per-ID disposition is in [BASELINE_FAILURES.csv](BASELINE_FAILURES.csv).

| Retained group | IDs | Evidence / disposition |
|---|---:|---|
| Missing pinned native dependency | 16 | Later selected protocol-native cohort passed with verified local Mihomo; does not provision hosted CI. |
| Missing canonical provider schema | 19 | Later selected fixture correction PASS; schema assumptions must stay deterministic/current. |
| Stale fake signatures | 4 | Later selected fake-signature correction PASS; adapters need explicit current contracts. |
| Missing synthetic runtime incarnation | 2 | Later selected fixture correction PASS; never remove real incarnation fencing for tests. |
| Unclassified contract/behavior | 6 | Last failed observation; exact returned result/cause remains partly unavailable. |
| Read-only lifecycle side effect | 4 | Last failed observation; source contains a watchdog getter with ensure-INSERT. |
| Xray lifecycle counter/result shape | 1 | Last failed observation expects missing `deleted_count`; contract decision required. |

The groups total 52. They are inherited classifications, not results at `eaecab5`. Among the six unclassified IDs, one additional Xray snapshot failure also records a watchdog row insertion: five IDs in total have this observed delta. `watchdog_runtime_state.py:28–35,57–60` proves the getter can insert the default row. This supports a concrete source investigation; it does not independently prove which request triggered every historical failure. **Do not pre-seed or weaken assertions solely to erase a read-side write.** Establish request/startup ownership first.

The remaining 11 include bootstrap selector result-shape/readback assumptions, two Mihomo unchanged-config tests, topology batch fallback expectations, a Mihomo log no-op result, five snapshot/lifecycle watchdog deltas and one Xray counter-shape case. Three of these have an additional exact baseline comparison in the retained operational report: clean `e49510c` and `c1e00b5` both failed the same IDs. That proves those historical failures preceded the operational changes; it is not a fresh main run or a waiver.

Historical UI `ux-presentation` observation was subsequently corrected at the fixture level and has selected PASS evidence. Do not reopen it automatically or label current UI regression without a new exact reproduction.

The older runs used pytest 9.0.3 while the product dev constraint is `<9`; the hosted dependency file pins pytest 8.3.5. Historical full-suite totals lack completely comparable environment evidence. No current product regression, environment fix or flake frequency is asserted from ID equality alone.

## 7. Runtime, resource and timing characteristics

No test resource workload was started on production. New CPU/RSS/I/O/WAL/temporary-file/parallel-worker measurements are therefore **unmeasured**, not zero. Static analysis and JSON parsing costs are not reported as test performance.

| Evidence | Time | What it does and does not establish |
|---|---:|---|
| Existing main hosted job `37705115129`, `eaecab5` | 29 s | Whole job including setup/smoke/docs selection; success, not full application/native suite acceptance. |
| Existing PR hosted job `37705002330`, `a5b403c` | 27 s | Whole documentation PR job; success, not the required future gate matrix. |
| Existing main hosted job `37698048850`, `e49510c` | 27 s | Failed affected-gate step; exact failure log/artifact unavailable, cause unclassified. |
| Historical 35 non-native exact IDs, `30a43a0` | 5.97 s | 35 failures before test-only corrections, pytest 9 environment; not current timing. |
| Historical 17 native-related IDs, `30a43a0` | 2.05 s | 16 PASS / 1 failure with existing local binary; not hosted provisioning. |
| Retained clean `e49510c` three-ID cohort | 1.65 s | Three historical failures; no resource peaks retained. |
| Retained operational native component cohort | 3.382 s | Two component checks, not full application CRUD/recovery. Exact execution source must be retained separately from report commit. |

These samples do not justify p50/p95, startup speedups or before/after claims across different selections. The historical 172.56-second L6 baseline is separate earlier evidence, not permission or a budget measurement for current L6. [Timing evidence](TIMING_EVIDENCE.csv) binds every retained sample to its source/environment limitation.

All 150 catalog duration fields are unmeasured. Serial coordinator deadlines/output caps exist; native Xray container caps exist; none establish complete Python CPU/RAM/temp-disk enforcement. Fixture-identity strings do not supply full content hashes. Installer temp roots can remain on direct invocation. The expensive full-application lifecycle budget remains unknown.

Safe optimization candidates for Phase B are exact shared setup duplication, repeated canonical DB initialization, named UI cases and immutable runtime/build reuse. Keep fresh writable per-test DB state; immutable schema templates are a candidate only after restore/migration isolation is proved. Do not share mutable caches/adapters/job managers across parallel suites.

Parallelism is **not accepted yet**. Candidate separation is one isolated process per read-only/unit file with owned paths; native Compose scenarios are serialized initially. Measure teardown, locks, resource ceilings and nested workers before increasing concurrency. Sub-second wall-clock thresholds and readiness polling require hosted repeatability evidence rather than longer sleeps or blind retries.

### Hosted platform facts and experiments still required

Repository visibility was read as public. Official GitHub documentation lists public `ubuntu-24.04` x64 with four CPU, 16 GB RAM and 14 GB SSD capacity; this is not measured free disk. Standard public runner usage is free, while account/storage/concurrency limits still need explicit budget checks. See [runner specifications](https://docs.github.com/en/actions/reference/runners/github-hosted-runners) and [Actions limits](https://docs.github.com/en/actions/reference/limits).

Phase D/E must observe actual free disk, image-layer growth, Compose startup/native validator version/checksums, artifact/cache retention, effective cgroup limits, local networking, browser availability and teardown on the chosen runner image. Do not use container-only `ubuntu-slim` as proof of Docker/kernel/systemd compatibility. No project-owned VM, self-hosted runner or Kubernetes is proposed.

## 8. Prioritized Phase B–E execution plan

These are proposals for operator review, **not authorization or started implementation**. One milestone branch remains the working unit; no CD task is included.

| Group | Phase / priority / dependencies | Work and acceptance criteria |
|---|---|---|
| B1 — Isolation bootstrap | B / P0 / first | Set test-owned state/config/run/log/temp and dotenv policy before application imports; assert capability/path ownership; no scheduler side effects by default; teardown waits are asserted. Negative tests prove default env/lock/prod path attempts are refused. No production invocation. |
| B2 — Assertions and baseline contracts | B / P1 / after B1 | Replace/retire `assert True` diagnostic; resolve read-side mutation ownership; triage all 11 exact IDs under pinned comparable environment; correct proven fixture/contract mistakes only. Product defects go to architecture owner, never fake a PASS. Each ID has evidence/owner/remediation; no blanket exemption. |
| B3 — Planner/metadata/gate standardization | B / P1 / B1 | Dependency-complete closure/cycle rules, Core/shared L5 policy, committed-range L0 checks, mixed-file node metadata and named UI scenarios. Contract tests prove unknown/shared changes cannot silently omit required coverage. Baseline skip/new/disappeared statuses remain explicit. |
| B4 — Fixture and resource standardization | B / P2 / B1 | Merge only exact shared setup semantics, content-address fixture/schema/native inputs, bound/measure setup and cleanup, replace scheduler-assumed overlap with deterministic event evidence where needed. Preserve empty-schema/migration and negative cases. Report comparable time/RSS/I/O before any speed claim. |
| C1 — Full application Xray lifecycle | C / P0 acceptance / B1–B3 | Implement isolated API/job/DB/Mihomo/Xray Compose application CRUD, fixed routes, loaded identities/bindings/export, repeated no-op and negative readback. Pass the complete mandatory matrix, not just adapter CRUD. |
| C2 — Crash/fencing/recovery | C / P1 / C1 | Controlled process/container crash/restart at checkpoint/apply/readback/publish; competing revision/incarnation; partial restore; no blind replay; last-good/no false success. Distinguish container/application tests from privileged systemd/kernel L7. |
| C3 — Core/provider/Mihomo/read-only smoke | C / P1 / B1 | Actual canonical state projection with independently expected values; zero provider requests for normal reads/selector/UI; mocked timeout/503/429/unknown/ambiguous mutation; preserve exclusive intent and fixed targets. Native routing integration uses synthetic isolated inputs. |
| C4 — Browser acceptance | C / P2 / B1 | Minimal real browser RU/EN, navigation, render/console/request behavior and mutation-free reads. Reuse a qualified immutable browser artifact; no production CDP session or secrets. VM/source tests remain unit contracts. |
| D1 — Hosted build/native provisioning | D / P1 / B/C contract agreement | Exact Python/Node and immutable Mihomo/Xray/browser provisioning, hash/digest/version evidence, bounded caching/build once, disposable Compose resources. Missing native inputs block the required gate. Verify disk/account budgets rather than assume capacity. |
| D2 — Workflow matrix | D / P1 / D1 + B3 | Push L0–L1; PR dependency-complete affected L0–L5; main integration/native smoke; L6 nightly/manual/release; L7 separately qualified release. Read-only CI authority, no production secrets or automatic deploy. |
| E1 — Actual runs and failure acceptance | E / mandatory / B–D | Real hosted runs with exact SHA/manifest/plan, selected node results, coverage limitations, duration/resources/artifacts, stable repeats and failure classification. Inject only isolated test failures; prove cleanup and required-native failure handling. |
| E2 — Hosted L7 compatibility register | E / release gate / D1 | Qualify each privileged/systemd/cgroup/routing/crash scenario. Unsupported scenarios remain named blockers or need a separately approved compatible gate; no env-string/dry-run or mocks as L7 evidence. |

Recommended order is B1 → B2 → B3 → B4 → C1 → C2 → C3 → C4 → D1 → D2 → E1 → E2, with only explicitly independent documentation/fixture review parallelizable. Required safety and functional coverage precede runtime acceleration.

## 9. Readiness and stop conditions

The later test-system acceptance requires:

1. Every suite/node and helper boundary is cataloged with meaningful assertions, domain/criticality, fixture owner and executable gate; dynamic parameter counts and coverage limitations are retained.
2. Pre-import isolation and owned path/resource cleanup are proved in the intended ephemeral environment; no production authority is available to PR tests.
3. Historical baseline IDs are reproducibly classified/remediated under the declared runtime. New or unclassified failure blocks acceptance; known IDs are not automatic exemptions.
4. Mandatory full application Xray CRUD/crash/recovery/native applied parity passes; job acknowledgements are not confused with verified terminal success.
5. Required native and affected-domain coverage cannot silently skip, and actual hosted runs prove the push/PR/main policies.
6. Measured suite time/resource/artifact budgets exist; reuse/parallelism does not sacrifice independent state or negative evidence.
7. Unsupported L7 scenarios have an explicit release disposition approved separately; no unverified gate is marked complete.

**Phase A ends here.** No test refactoring, workflow/CD implementation, performance fix, production operation or Phase B transition is authorized by this report. Await the user's review. Operational Performance Fixes remain on their unchanged branch and cannot merge until the previously agreed full application acceptance is complete.
