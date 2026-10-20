# Test Architecture and CI/CD Foundation

## Current checkpoint — 2026-10-15

The CI milestone is CLOSED following actual squash/main validation (PR5, main run38363962787). PR8 merged as dabf9f4; PR10 merged/deployed as ffe506e. Production remains ffe506e with the original78 identities restored and API/Gateway recovered. PR11 isolation/redaction fixes are source-only, not deployed: Synthetic Functional Acceptance PASS, Production State Acceptance PARTIAL, controlled external E2E NOT RUN, overall Production Acceptance BLOCKED. These are recorded receipts, not new live probes. Earlier dated checkpoints below are historical where superseded; they do not reopen CI or describe the current held/recovery state.

Use the [active roadmap](../ROADMAP.md) and [decision register](DECISIONS/ROADMAP_DECISION_REGISTER_2026-10-15.md) for priorities, dependencies and remaining evidence. Operational Performance requires a fresh audit after PR8/10/11 before replaying old optimization packages. Full Debian host/systemd/reboot and positive external E2E remain separate unqualified gates. No implementation or production action is authorized by this documentation reconciliation.


## Historical PR readiness — 2026-10-13

PhaseE PRE-MERGE ACCEPTED at actual PR5/run38347054113:1429/0/0, qualified26+1, ordinary26/Provider11/packet1, strict receipts/cleanup and historical7/7. Squash-only/auto-delete and existing Main required stable Actions15368 check now configured with no bypass. Latest protected PR HEAD must pass before user Squash and Merge; post-merge main validation remains OPEN, CD DEFERRED and production UNCHANGED. Earlier dated BLOCKED/settings-pending pointers below preserve pre-PR history, superseded only by [final handoff](../audits/test_architecture_phase_e_2026-10-13/FINAL_HANDOFF.md). No agent merge or deploy.

## Roadmap consolidation — 2026-10-13

Authoritative execution plan: [engineering mirror](../ROADMAP.md), synchronized with `/решения/roadmap/fwrouter/ROADMAP.md`. PhaseA/B completed; PhaseC complete in agreed scope; PhaseD ACCEPTED; PhaseE/overall BLOCKED on final GitHub promotion. Existing reports/evidence remain unchanged. No new execution/CI or deployment is authorized by this documentation update.

Real PR event and authorized required-check/squash configuration are pre-merge gates. Actual main-event verification follows operator-approved Squash and Merge; it cannot be required before the event exists. Milestone acceptance remains open until that sequence is confirmed. Future configuration/security matrix, DNS optimization, rescue0 audit, public documentation, server-wide cleanup and Debian host/production-live release gates belong to their owning roadmap stages, not new artificial blockers of accepted PhaseD. Do not duplicate their detailed backlog here.

Git policy: one task/milestone branch and PR → required CI PASS → separate operator review/authorization → one single-parent squash commit in main; completed merged head deletion is targeted, unfinished branches untouched. Main merge/rebase methods must be disabled and stable required affected status enforced only after settings authorization. Auto-delete is already enabled per PhaseE read-only evidence. CI/merge ≠ deploy; CD DEFERRED, manual exact-release deployment and Production Live Acceptance separately approved.


## Current acceptance pointer — 2026-10-13

**Phase D: ACCEPTED (hosted implementation/mandatory gates). Entire Test Architecture & CI Stabilization milestone: OPEN; Phase E technical review complete; BLOCKED on true PR/main events and required-check/squash promotion.** Source `a10dd9c`, [run38305978196](https://github.com/Coxf0rd/fwrouter/actions/runs/38305978196): affected1429 PASS/0 FAIL/0 SKIP; qualified26+1 and minimal application/native1 PASS; ordinary26/Provider-kernel11/packet1 PASS twice with exact source/plan/native/capture/cleanup receipts. Historical exact-seven7/7 PASS, no baseline allowlist. Targeted cache/prewarm/state300 nodes PASS five times (38305595288). See Phase D REPORT and `FULL_BUNDLE_CHECKPOINT_38305978196.json` for scope/resource limits.

L0–L4 primary proofs plus L5 affected-regression anchors; no L6/L7 execution. GHCR digest-pinned Debian12 base and fresh-source consumers confirmed; Compose is not Debian host/reboot acceptance. Actual PR/main event execution and protected required-check promotion remain for authorized final CI acceptance. No production changes/deploy or CD. Operational Performance Fixes remains PAUSED/BLOCKED in its preserved branch. Phase E is authorized; do not begin another milestone automatically. Earlier checkpoint/status sections below preserve history and are superseded only within the exact scope above.


## Current full hosted checkpoint — 2026-10-13

Run [38291414020](https://github.com/Coxf0rd/fwrouter/actions/runs/38291414020), source `dfa583f`: ordinary26, Provider/kernel11 and packet1 all PASS twice with distinct owned receipts, no FAIL/SKIP and clean cleanup. Qualified Mihomo/child26, native Xray1 and minimal application/native1 PASS. The current affected unit leg is1419 PASS/2 FAIL/0 SKIP; all historical exact-seven are included and PASS. Overall run remains FAIL: selective IP-only candidate assertion and installer dependency dry-run root requirement remain under targeted attribution. Unit CLI correctly returned1, but the workflow wrapper omitted propagating that exit and unnecessarily ran the hosted bundle; strict final aggregation still rejected the failed unit row. No false aggregate acceptance is claimed. Reviewed bounded evidence: `FULL_BUNDLE_CHECKPOINT_38291414020.json`; raw evidence remains untracked. Next gate is narrow fixes/verification, then complete source-matched aggregation. Phase D BLOCKED; production/Phase E/CD/L6/L7 untouched. Earlier checkpoint notes are historical scoped evidence.

## Current hosted scope — 2026-10-12

Expanded isolated kernel/packet run [38249879539](https://github.com/Coxf0rd/fwrouter/actions/runs/38249879539), `74bd48f`, is **1 PASS / 0 FAIL / 0 SKIP** (59.189s): selective DNS priority, actual public Auto switch to second native VLESS egress, TCP/UDP/DNS observations, zero forbidden DIRECT leakage/extra provider calls, native/Core parity and exclusive Provider restoration all complete. Focused generation-input/fence regressions12/12 pass. Derived active selection is excluded only from structural config input; independent selection revision/operation/incarnation/config fences remain mandatory. Earlier marker, selective preparation/egress and unverified fast-default mutation defects are closed only within this hosted scope; their chronology remains in the report.

Provider cohort38208049662 is11/11 PASS. Ordinary fresh-source GHCR application/Xray38210552876 is1/1 PASS and independent historical exact-seven7/7 PASS. Kernel/packet GHCR migration is confirmed by publication38254752823, anonymous consumer38255208001 and expanded packet38255994863 (1/1 PASS). The same source-free pinned base plus fresh-source overlay is used, with exact package/native/script readback and unchanged confinement. Qualified run38250409971 failed before collection on source-manifest mismatch; both verifier inventories are fixed in4a7b823, repeat38256549625 confirms qualified Mihomo/child26/26 and Xray native1/1 PASS without skips, source-manifest regression1/1 and owned cleanup. Schema acceptance does not prove packet reachability for every adapter. Full functional/affected and reproducibility remain open. Phase D BLOCKED; production, Phase E/CD and L7 untouched. [Phase D report](../audits/test_architecture_phase_d_2026-10-09/REPORT.md) retains historical findings and exact evidence references.


Latest broad run38257165004 is BLOCKED: routine1324 PASS/114 FAIL and three missing runner adapters; actual functional33 PASS/5 FAIL with38 collected. The common profile-flattening defect is corrected in source by exact ordinary26 + Provider/kernel11 + packet1 partition, not by adding capabilities to ordinary tests. Ambiguous legacy functional commands/receipts are rejected. A24-file explicit unit fixture validation cohort is prepared for hosted-only execution; source changes and contract assertions remain pending remote proof. Qualified receipt results may classify nodes only after complete source/native/JUnit/cleanup verification. No blanket baseline exemptions, production changes or Phase E/L7 acceptance.

## 2026-10-12 focused native re-entry checkpoint

Hosted run [38207720641](https://github.com/Coxf0rd/fwrouter/actions/runs/38207720641), source `35a7353`, passes both exact native re-entry scenarios (**2 PASS / 0 FAIL / 0 SKIP**, JUnit suite 34.995 s) after focused preflight (46 gate, 94 acceptance, 38 network-fixture, 13 smoke contracts). Both the concurrent exclusive-intent race and real Mihomo-incarnation-change check pass; each current member probe is HTTP 200, 2 ms, `probe_ok=true`. This closes only those two re-entry cases. It does not prove provider-cohort, or packet forwarding after an actual VPN-auto selector change. Resource receipt: wall 35.008 s, CPU user/system 18.975/2.552 s, maximum process RSS 119844 KiB (per-process high-water mark), temporary bytes 2177688 before cleanup; cleanup errors empty. No production behavior changed. Phase D remains **BLOCKED**; see the [Phase D report](../audits/test_architecture_phase_d_2026-10-09/REPORT.md).

## 2026-10-12 native LAN DNS packet acceptance

Hosted run [38181603796](https://github.com/Coxf0rd/fwrouter/actions/runs/38181603796), source `fa0056e`, passes the current real packet scenario (1/0/0; JUnit suite 60.898 s), including native DNS/53, actual LAN UDP/TCP answers, external-DNS capture, nftset materialization, TCP/UDP flows through VPN and zero forbidden DIRECT deltas. Fixed test-only DNS flag/guard/projection defects were corrected without weakening runtime assertions; all native proof remains mandatory. Readiness alone was never acceptance. Preflight 46 gate / 93 acceptance / 38 network-fixture contracts. [Exact bounded receipt](../audits/test_architecture_phase_d_2026-10-09/PACKET_CHECKPOINT_38181603796.json). Global-VPN DNS acceptance supersedes historical DNS NOT RUN notes only in that scope. Actual selector-change packets, selective-mode priority, concurrent re-entry cases, fresh full/historical gates, GHCR application consumption and reproducibility remain open. Phase D BLOCKED; no production, Phase E/CD or L7 execution.

## Current targeted checkpoint — 2026-10-11

Phase D remains **BLOCKED**. Run `38159841586` executes the exact two hosted native re-entry cases after bootstrap passes (gate contracts 46/46; acceptance contracts 89/89); both fail native verification (0 PASS / 2 FAIL / 0 SKIP). At each barrier, an owned HEAD/204 response intent and a separate local `end_headers()` completion are observed, but neither proves peer receipt. Mihomo's exact `extra[test_url]` sample is present and fresh with `alive=false`, delay 0; the unchanged helper rejects on `alive`. Pinned v1.19.31 source shows zero delay alone does not set the sample dead; with the generated nil expected-status range, `alive=false` requires a URLTest error/no accepted response. An immediate same-URL fallback health check can also write that sample concurrently; this run does not attribute the writer or cause. The re-entry receipt establishes no provider success or remote DOWN and does not attribute a production defect. Separately, run `38159521000` identifies a stale cached-`None` effective-artifact read blocking the DNS53 daemon restart; source `0fda575` reaches the native fixture in run `38160582405`, but readiness times out. The receipt's `nftset_probe_ok=true` is a boolean and does not establish native nftset proof; DNS packet acceptance remains open. Earlier packet PASS receipts remain scoped evidence, not full DNS acceptance.

GHCR publisher and independent anonymous digest-pinned consumer are now verified: run `38152526883` cold pull 28.042 s, warm pull 0.060 s, dependency image 2,598,192,591 bytes. This supersedes only older publisher/consumer-pending statements; application source-overlay migration and Docker-root headroom acceptance remain OPEN. Full functional/affected gates, current seven-ID recheck, packet proof after an actual selector switch and reproducibility remain OPEN. No Phase E, CD, L7, production deployment or runtime changes. See the Phase D REPORT for source/run/evidence boundaries; historical checkpoints below are retained.

## Verified protected-DIRECT checkpoint — 2026-10-11

Run [38141202543](https://github.com/Coxf0rd/fwrouter/actions/runs/38141202543), `cf62b37`: **1 PASS / 0 FAIL / 0 SKIP**, 46.575 s. Real protected TCP/UDP forwarding succeeds under stable active VPN with exact endpoint/capture/native rule/set/counter proof. The prior UDP/empty-pool/Emergency Direct/re-entry checks also pass. Run `38140783732`, `49d40ff`, separately confirms **3 application/native/browser PASS** (including real CAS-miss fresh reconciliation) plus 18 read-model and 4 alias cases. This supersedes the older protected-DIRECT and targeted CAS/browser pending notes only in the tested scope. DNS/53, actual selector-change packet forwarding, provider cohort, full functional/affected, GHCR and reproducibility remain open. Phase D **BLOCKED**; production unchanged. See Phase D REPORT and `PACKET_CHECKPOINT_38141202543.json`; older evidence is retained.

### Packet acceptance update — 2026-10-11

Run [38138693984](https://github.com/Coxf0rd/fwrouter/actions/runs/38138693984), `8689e23`: **1 PASS / 0 FAIL / 0 SKIP**, 55.400 s. Actual supported/unknown VPN UDP paths, unsupported/unavailable/empty-pool fail-closed, TCP, Emergency Direct and verified VPN re-entry pass. Re-entry provider calls are 1→1 (zero additional calls); native handoff/rollback receipt proof and cleanup pass, with unchanged host network/firewall snapshots. This supersedes the pending alias acceptance below only for this packet scenario. DNS/53, protected DIRECT, packet proof after a real selector change, fresh application/browser/provider/full gates and GHCR remain open. Phase D **BLOCKED**, production unchanged. See the [structured checkpoint](../audits/test_architecture_phase_d_2026-10-09/PACKET_CHECKPOINT_38138693984.json).


## 2026-10-11 — current bounded UDP/recovery checkpoint

Phase D remains **BLOCKED**. Tri-state UDP and mandatory-VPN fail-closed source are implemented on the CI branch; production is unchanged. Run [38136295650](https://github.com/Coxf0rd/fwrouter/actions/runs/38136295650), `b2ca8d9`, passed **48 exact unit/component cases / 0 FAIL / 0 SKIP** (5.208 s), including native-proof receipt ownership and honest incomplete rollback outcomes. Packet run [38136831776](https://github.com/Coxf0rd/fwrouter/actions/runs/38136831776), `bda38c9`, remains **0 PASS / 1 FAIL / 0 SKIP**. Its trace proves failed Mihomo handoff binding/readback because `172.18.0.1` is absent from the router namespace; receipt publication correctly is not reached. The namespace-only correction `62ef4a4` awaits hosted acceptance. Scoped packet phases do not constitute complete packet acceptance. Port-53 DNS-policy proof, fresh full functional/affected/historical gates and GHCR publisher/consumer remain open. Dated DRAFT/NOT RUN text below is historical and superseded only within the stated source/unit scope; Phase E/CD and the operational branch remain untouched.


Foundation checkpoint (`178b471`): Source/Tests/Commit completed; current Phase B gate standardization is recorded in [GATES_REPORT.md](../audits/test_architecture_phase_b_2026-10-08/GATES_REPORT.md). The current manifest classifies 162 test files (151 at the Phase B checkpoint). Hosted workflow execution, locked-environment acceptance, native runtime provisioning, deploy authorization/protected deployment, and disposable L7 acceptance remain separate unverified gates. No Phase B edit deploys or restarts production. See the [foundation implementation report](../audits/test_architecture_cicd_2026-10-04/REPORT.md) and Phase A audit for historical details.

Current milestone status: Phase D implementation is separately authorized and has hosted execution. Full run [37934872853](https://github.com/Coxf0rd/fwrouter/actions/runs/37934872853), head `b580b2e`, completed BLOCKED: affected L0–L3 had 1,112 PASS / 129 FAIL with L4/L5 absent/incomplete; the 37-case functional cohort had 23 PASS / 14 FAIL. Qualified Mihomo 26/26, isolated Xray 1/1, and minimum application Xray readback 1/1 passed but do not close those mandatory gates. Earlier singleton run [37934437529](https://github.com/Coxf0rd/fwrouter/actions/runs/37934437529) passed 1/1; prior seven-ID run [37904598829](https://github.com/Coxf0rd/fwrouter/actions/runs/37904598829) passed 7/7 as scoped historical evidence, with five current PASS and two skipped/uncollected. Phase E remains OPEN. No production deployment/restart or CD occurred. See [current checkpoint](../audits/test_architecture_phase_d_2026-10-09/REPORT.md), [structured receipt](../audits/test_architecture_phase_d_2026-10-09/LAST_FULL_CHECKPOINT.json), and [failure analysis](../audits/test_architecture_phase_d_2026-10-09/FUNCTIONAL_FAILURES_37934872853.csv). Dated foundation and Phase C sections below are historical snapshots.

## 2026-10-11 focused UDP unit and L0 result

Run [38114764935](https://github.com/Coxf0rd/fwrouter/actions/runs/38114764935), commit `ef06e54`, passed the changed-Python L0 F821 check and the fixed 12-node UDP capability/Mihomo backend regression set (**12 PASS / 0 FAIL / 0 SKIP**). The F821 check uses hash-locked Ruff 0.17.0 on changed Python paths only, runs with `--ignore-noqa`, and self-checks one valid and one undefined-name fixture in the hosted bootstrap. This is narrow L0/L1/L2 evidence: it does not run native packet acceptance or prove packet forwarding, and those native checks remain pending. The historical full-gate totals above are unchanged. Phase D remains **BLOCKED**; no production deployment or restart occurred.

## 2026-10-10 kernel and packet checkpoint

Run [38036768080](https://github.com/Coxf0rd/fwrouter/actions/runs/38036768080),
source `37373a6`, confirms two real Core/native recovery and selector-fence
scenarios: **2 PASS / 0 FAIL / 0 SKIP**. The strict kernel profile has actual
nftables/TPROXY/policy-route preflight and exact native readback. These two
scenarios do not establish LAN packet forwarding or supersede failed broad gates.

The next explicit `packet-diagnostic` L3 scenario reuses the Compose harness,
Debian dependency layers and fresh commit source for LAN/router/local-endpoint
roles on two internal networks. All roles have reviewed `NET_ADMIN`; only the
router gains `NET_RAW` for bounded header-only capture. Profile, namespace,
network membership, routes, egress guards and cleanup must qualify before
application acceptance. See [Network Testbed contract](../../tests/acceptance/NETWORK_TESTBED.md).
Packet source is under review; hosted execution and packet acceptance are
**NOT RUN** at this checkpoint. Full functional, affected, reproducibility,
GHCR publication/consumption and current seven-ID recheck remain open.
Debian host/systemd/reboot/physical-interface parity is a separate release gate;
Phase E/CD remain unstarted and production remains untouched.

## Test levels

| Level | Contract | Typical evidence |
|---|---|---|
| L0 — static | Syntax checks, pinned Ruff full F/E9 across maintained tracked Python and new/changed Python; schema/migration compilation and other affected static policy checks. | Same bounded fail-closed lint entrypoint locally and in existing push/PR/main jobs before routine/native execution. The maintained F/E9 baseline is clean; explicit export contracts preserve facades. Ruff does not prove arbitrary import/interface compatibility. |
| L1 — unit / white-box | Implementation-level functions, state transitions, pure transforms and bounded failure semantics. White-box checks may know internal structure and are not by themselves public contract evidence. | Deterministic isolated fixtures; no production state or network. |
| L2 — public/component contract | Black-box assertions against externally observable API, job, event, persistence and component behavior. Keep assertions independent of private implementation details where possible. | Real component boundary where practical; fakes are explicit at external boundaries. |
| L3 — integration | Compose real isolated components across service, temporary SQLite/config/artifact files and runtime adapter boundaries. | Prefer real isolated components; use deterministic fakes only for external provider/network/process boundaries that must be controlled. |
| L4 — smoke | Narrow startup/readiness and one representative public behavior for the changed deployable component. | Disposable or isolated environment; no production mutation. |
| L5 — affected-domain regression | Tests across the changed subsystem and its directly connected contracts. | Explicit affected-test selection plus stable baseline classification. |
| L6 — full suite | Full repository regression gate. | Milestone, nightly, release, broad shared-contract change, or explicitly approved major checkpoint—not every individual fix. |
| L7 — staging failure/recovery | Deliberate failure injection, restart, recovery, rollback and observability checks. | Disposable isolated staging only; never production. |

## Default test selection

The checked-in `tests/gates/manifest.json` gives each test file one primary level (L0 is the independent static gate). File defaults and exact node overrides carry domain, dependency, fixture owner/identity, isolation, network/native/slow/destructive flags, timeout and output/artifact bounds. `tests/gates/gate.py plan` maps immutable changed paths through reviewed domain rules, then selects the transitive dependency closure. Cycles terminate through a visited set; unknown paths and dependency names fail closed. Native suites needed by an affected source path stay mandatory even when their pinned runtime is unavailable; absence blocks the result instead of becoming a pytest skip. Suites that need real child-process execution carry an explicit execution profile. If no qualified profile is available, the gate reports a blocker and does not start the suite or its native preflight; `--include-native` and environment strings do not grant that profile.

L5 is a fixed set of reviewed regression anchors selected for explicitly shared domains, including Core; its tests retain their L1–L4 primary level. Core owns canonical writer, applied-state and public projection boundaries, so Core changes select the shared anchors. L6 is a full-manifest plan available only through explicit manual/nightly/release invocation. L7 has only a dry-run policy until a disposable staging attestation runner exists. A mock, local service, or attestation environment string cannot establish L7.

Do not run L6 after every small edit. For every implementation report, state the levels run and why. Default to affected L0 plus affected L1/L2 and required L3/L4; select L5 when shared/domain contracts change; run L6 only at scheduled/manual, milestone, release, major architecture, or explicitly required policy gates; run L7 only in disposable staging/release acceptance. Expand coverage when a failure, changed contract, broad dependency or release gate justifies it. Do not weaken correctness, recovery, data integrity or resource limits to save test time.

## Isolation, determinism and resource limits

- Tests must not access production SQLite, live dataplane, systemd, production Docker containers, provider accounts, or public networks unless explicitly marked and separately authorized as a live acceptance test.
- Use temporary database/config/artifact paths, unique pytest basetemp directories, deterministic fixtures and bounded fake clocks/processes. Tests that require a native binary use an existing pinned version and isolated synthetic inputs; do not install packages or pull mutable images automatically.
- Keep subprocess/network deadlines, output size, process count, CPU/memory, temporary storage and parallelism bounded. Clean up only test-owned processes, containers and files.
- Manifest rows classify live, destructive, network, native, slow and staging requirements. The plugin refuses live/destructive collection in routine runs; staging remains deny-by-default until a real artifact verifier is implemented.
- Fixture contract: every suite has a named owner and identity in the manifest; mutable state uses unique owned paths and explicit cleanup. Prefer real temporary SQLite/schema behavior for persistence contracts, and mark fakes at external boundaries such as providers, subprocesses or the partial system projection. A missing dependency, fixture, report or cleanup receipt blocks/fails the suite; it cannot become an empty green result.
- The gate runner starts serially, uses a clean child environment with test-owned state/temp paths, disables schedulers, strips inherited FWROUTER/provider/proxy settings, bounds each process deadline and captured output, and terminates the owned process group on timeout.
- L0 whitespace validation checks the immutable committed `base...HEAD` range recorded in the plan, including when the checkout worktree is clean.
- Reproduction instructions state the exact command, repository commit, runtime/tool versions, fixture identity and environment variables that affect selection.
- Keep CPU, RAM, wall time, parallel workers, temporary disk, native process count and container resources bounded. Resource limits are part of test correctness, not only CI cost control.

### Credential provenance and live-state boundary — 2026-10-15

Fixture identity must be independently synthetic: use reserved test namespaces/documentation addresses and generated fixture credentials, not literals copied from DB, backups or real subscriptions. Sanitized real API captures must declare their origin, remove credential values and be consumed through an explicit controlled transport. String overlap, display names and fixture-sized record counts do not establish origin. Inbound Xray client identities, provider upstream credentials and subscription bearer tokens are separate security scopes.

Live-state reads of real profiles require explicit authorization and private bounded evidence; they are not synthetic tests or proof of upstream E2E. Loopback smoke ignores ambient proxies and rejects redirects. Subscription gateways must never log raw paths/query strings, identities or arbitrary error/request text; retain only allowlisted method/status/event metadata. Synthetic canary regressions verify these boundaries. A historical credential disclosure is not revoked by removing a fixture from current HEAD; ownership assessment and any rotation require a separate targeted plan. See [2026-10-15 findings and qualification](../audits/test_isolation_live_acceptance_2026-10-15/REPORT.md).

## Baseline classification and durable evidence

A failure may be called baseline only after reproducing the exact node ID on the declared parent/baseline under a comparable environment. Separate pre-existing failures, environment/fixture failures, skipped tests, fixed baseline failures, novel failures and unclassified failures. CI must stop promotion on any novel or unclassified failure. A known baseline failure may be accepted only through a narrow, explicit and reviewable exception backed by durable classification; do not report raw totals as green. Never silently exclude a raw test failure or broaden a test filter to hide it.

`knowledge/audits/test_architecture_cicd_2026-10-04/BASELINE_STATUS.json` stores all 52 exact historical IDs and triage; it is evidence input, not an allowlist. `approved_baseline_exception` is false. Its historical summary records 41 IDs with later selected PASS evidence and 11 with a retained last failure; that is not one complete post-fix rerun. The Phase B exact-ID follow-up reports PASS evidence for four of those 11 and leaves seven unresolved; this is a narrow 11-ID observation, not a 45-ID single run or a complete suite ([Phase B report](../audits/test_architecture_phase_b_2026-10-08/REPORT.md), [backend detail](../audits/test_architecture_phase_b_2026-10-08/BACKEND_REPORT.md), [per-ID ledger](../audits/test_architecture_phase_a_2026-10-08/BASELINE_FAILURES.csv)). The classifier reports newly failed, unapproved historical, passed-now, skipped/uncollected, retired-with-evidence, and unselected-pending IDs separately. A baseline ID is fixed only when that exact ID executed and passed. Approval exceptions require owner, remediation, evidence, and an unexpired date. Reports bind source commit, plan digest, manifest digest/version, tool version and exact selected suite coverage; promotion eligibility does not itself deploy.

The runner report contains per-suite timing, tool versions, fixture identity hashes, bounded output byte counts and exact node statuses; raw test output and environment values are not retained. The Linux gate and smoke entrypoints create their parent temporary roots explicitly under `/tmp`, independent of inherited `TMPDIR`. Each pytest suite gets a fresh private coordinator root below the gate-owned temporary root, with a validated marker, its own state/cache paths, basetemp and exact-node report. The coordinator removes the outer root only after consuming the report. CI report paths are placed in the hosted runner's temporary directory, then uploaded as commit-named artifacts with a 14-day retention limit. `tests/gates/requirements-ci.txt` pins the Python 3.11 test environment by exact versions and wheel hashes; `backend/pyproject.toml` remains the product dependency contract. Do not retain secrets, provider payloads, production configs, credentials, or raw identity material in test artifacts.

The isolated component smoke command is `python tests/gates/gate.py smoke --profile isolated`; it stages the backend only to a fresh temporary target, creates a canonical-schema DB inside a fresh child-owned state directory, and checks FastAPI TestClient Health/system routes with lifecycle disabled. The child loads the routine isolation bootstrap before application imports. Its state projection is a fixture, so this remains partial component evidence rather than full Core projection acceptance. A separate standard-library isolation bootstrap smoke is prepared for future hosted invocation; it exercises guards in a fresh Python child before application imports and does not claim full application L4 acceptance. Native Mihomo/Xray checks are `not_requested` by default. The explicit `--complete-native --validators-json <file>` option runs local binary/config validation; the standalone smoke path does not verify or enforce the gate runner's `qualified-child-process` profile, and this option does not grant that profile. Use it only as a separately authorized manual check in a disposable isolated environment with pinned binaries and synthetic configs; do not treat it as routine hosted or host-isolated evidence. The routine gate blocks process-dependent suites independently of this smoke option. A live-readonly run requires explicit `--opt-in`, reason, bare loopback API URL, DB path, both native validators with generated/mounted parity, and an exact failed-unit allowlist. No live profile runs in hosted CI.

For a manual single-domain/level cohort, use `python tests/gates/gate.py subset-plan --domain selector --level L1 --output "$RUNNER_TEMP/plan.json"` followed by `python tests/gates/gate.py run --plan "$RUNNER_TEMP/plan.json" --manual-subset --output "$RUNNER_TEMP/report.json"`. A subset report is explicitly non-promotable; it cannot satisfy deploy eligibility. L5 can be inspected with `dry-run --level L5`; L6 requires its own explicit manual plan; L7 remains a non-executing dry-run.

## CI/CD and deploy gates

CI should execute the declared levels in increasing cost and stop promotion on novel or unclassified failures. A known baseline failure may pass only under an explicit, narrow, reviewable policy exception with durable exact-ID evidence; never silently filter it or present raw totals as green. Run L6 at the milestone/release/nightly boundaries. Staging L7 artifacts and reports are separate from production deployment. Deployment must consume a reviewed immutable source commit, run the project installer/checks, and keep Source, Tests, Commit, Deploy and Live verification as separate records. No workflow may auto-run destructive production tests, provider mutations, runtime restarts, schema rollback or broad cleanup as a test side effect.

`.github/workflows/test-gates.yml` defines read-only hosted PR/push checks for L0 and deterministic affected selection. `.github/workflows/test-full-suite.yml` restricts L6 to a scheduled or explicit manual run. Both pin Actions by full commit SHA, use hosted runners, grant only `contents: read`, and have no production secrets or deployment steps. Historical foundation status at178b471: the workflows were source definitions only and remote execution was not claimed. Current PhaseD/PhaseE pointers and run receipts supersede that status only for their exact tested scope. L7 staging and deploy eligibility consumption remain separate gates. Full operational test/CI documentation is deferred to the Production Documentation stage.


### Later CI stabilization plan — 2026-10-08

The Source/Tests/Commit foundation at `178b471` is a closed checkpoint and is not reopened. The next active milestone is the separately planned **Test Architecture & CI Stabilization**, with audit, refactoring, missing coverage, hosted Actions and real-run acceptance phases. Its current contract is [CI_STABILIZATION_AND_GATED_CD.md](CI_STABILIZATION_AND_GATED_CD.md). GitHub-hosted runners only; no self-hosted runner or project-owned test VM. Existing Actions files do not prove remote CI acceptance. Operational Performance Fixes are PAUSED/BLOCKED until full application staging acceptance; their retained local branch is documented in the canonical roadmap. CD is a separate future milestone deferred until a new decision; production continues to use the manual installer. External telemetry/metrics and traffic accounting remain late after extraction and DB/architecture stabilization.

## Qualified integration/native execution boundary — Phase B review

Routine `backend/tests/conftest.py` is deliberately Python-only and fail-closed. It is not the harness for native application acceptance: its process/socket guard and default adapter fakes cannot establish integration parity. No environment label, pytest marker, binary path or CI detection may weaken it on a production host.

Before Phase C runs real subprocess/network tests, supply a separate integration test root and bootstrap in an independently isolated job/container. Keep unit fixtures out of that root; import the actual application only after isolation/path configuration. The outer hosted harness owns disposable containers, while the test/application container has no host Docker socket, host network, production bind mounts, credentials or production routes. Use synthetic identities, private internal Compose network (no outbound provider access), explicit ephemeral runtime endpoints, job-owned state/config/locks and pinned native binaries/images. Permit subprocesses and sockets **within that confinement**, with bounded time/output/process/CPU/RAM/disk limits and owned-process cleanup. Docker orchestration authority must not be passed into the application container.

The qualified profile must verify its environment and bind its receipt to the exact suite/source/image/config inputs; an unsupported or unverified profile blocks required suites. Phase B defined this contract; Phase C now prepares its implementation, but actual harness acceptance remains NOT RUN. Phase D may wire qualified execution into Actions only after source review. Pure host-systemd/reboot/kernel scenarios unavailable in the hosted environment remain explicitly unverified L7 gates, not substituted with mocked PASS.

Routine production capability-denial tests must continue passing alongside positive qualified tests demonstrating real owned subprocess execution, private service communication, native loaded-state readback and complete teardown. Never solve compatibility by an unrestricted `ALLOW_SUBPROCESS` or `CI=true` bypass.

## Phase C source completion — 2026-10-09

Status: **SOURCE COMPLETE / EXECUTION PENDING**. Checkpoint `c25ef4c` is preserved
in the linked historical report. The hosted native-process harness now has 31
application definitions / 48 exact cases: 37 functional L3 and 11 explicit L7.
Real Xray profile generation, eight durable checkpoint crash stages, CAS after
native readback, newer intent/source rows, joined Core/provider/Mihomo recovery,
SQLite and four real Chromium scenarios are registered. See the
[report](../audits/test_architecture_phase_c_2026-10-08/REPORT.md) and
[traceability](../audits/test_architecture_phase_c_2026-10-08/TRACEABILITY_MATRIX.csv).

`tests/acceptance/scenarios.json` plus source-only AST catalog bind exact IDs to
receipts. Stale/incomplete registries fail closed. Shared application changes
require functional hosted acceptance in the affected plan; the ordinary local
gate still refuses execution. UI requires browser acceptance. L7 never enters
normal affected selection and requires separate explicit recovery authorization.

60 pure local gate/isolation/smoke-contract checks PASS; this is not native L4
smoke. Actual native/application/browser/crash cases are **NOT RUN — pending
Phase D**. Source completion is not full acceptance or authorization to implement
Phase D. Production app/runtime/workflows and Operational `6004400` are unchanged.
Seven historical failures remain OPEN until actual corrected/replacement evidence.

Native process transport does not prove stock Docker/systemd/nft/kernel/reboot
or production startup parity. Test-only fault seams wrap original fsync/CAS and
real external transport; no synthetic successful service/native proof. CAS fault
injection advances the owned real SQLite revision, distinct from public-API
competing intent tests. Desired pending bytes may differ from loaded last-good
on failed reload; do not assert false convergence or discard persistent intent.

Run/dependency/receipt rules:
[hosted contract](/srv/fwrouter/tests/acceptance/RUNNING_ON_GITHUB_HOSTED.md).
No self-hosted runner/project VM, production secrets, CD or automatic deployment.

### Phase D stopped evidence checkpoint — 2026-10-10

Phase D remains **BLOCKED**. Scoped hosted checkpoints: metadata 9/9 PASS (37963072861), provider singleton 1/1 PASS (37963610399), explicit host-observation fixtures 6/6 PASS (37965069661), eleven-case provider cohort **2 PASS / 9 FAIL** (37965465706). Fresh numeric zero-delay evidence explains only part of the probe failures; recovery/CAS/browser acceptance is still open. Last full results are not superseded by scoped runs. Additional read-model fixtures and fence diagnostics are source-prepared, **NOT RUN**. No Phase E/CD, PR/merge, production deployment or runtime changes. See `knowledge/audits/test_architecture_phase_d_2026-10-09/REPORT.md` and `PROVIDER_COHORT_CHECKPOINT_37965465706.json`.

### Phase D target environment decision — 2026-10-10

The target host remains a standard GitHub-hosted Ubuntu runner. Application/native/browser components reuse the existing single isolated Docker Compose service with Debian 12 userland, pinned Python/native/browser versions and immutable base digest. This is not a Debian VM or host systemd/nftables/TPROXY proof. No production credentials, host network, privileged container or production Docker socket is allowed.

Dependency reuse is ordered after the current native/application blockers: publish a source-free dependency image to GHCR from a trusted publisher, consume an immutable reviewed digest, then build/verify fresh allowlisted FWRouter source for every checked commit. Publishing credentials must be restricted to the publisher job; PR/fork test jobs never receive package-write authority or production secrets. Keep one Compose service unless measurements justify a split. No Docker-image artifact upload as the primary registry. Current GHCR implementation/acceptance is **OPEN**.

Cold measurement in run 37970549010: application image 1,961,517,169 bytes; Compose build 45.328 s; base image absent before build. This is combined pull/build time, not a cache-hit metric. Warm pipeline, immutable dependency image size/download, BuildKit hits, aggregate CPU/RAM/disk peaks and before/after benefit still require measured acceptance. Respect runner resource/disk/artifact budgets; do not label planned caching as an improvement.

Narrow hosted evidence: strict zero-delay fake-only L1 5/5 PASS in 37969825404; explicit read-model fixtures 18/18 PASS in 37971905644. The latter native job was NOT RUN because stale scenario-description inventory correctly blocked preflight. Corrected native/application/browser results are pending. Latest full functional/affected receipts remain unchanged; Phase D BLOCKED, Phase E/CD/operational return not authorized.

Narrow fixture run38265018734: distinct353 PASS/24 FAIL/0 SKIP (reported354 includes a synthetic Node file-summary); Xray debounce21/21 PASS. Gate54 PASS; acceptance106 PASS/1 FAIL/1 ERROR. Complete native/functional acceptance remains open. Shared TMPDIR, Node receipt accounting and incomplete contract fixtures are under correction; unsafe subprocess is still denied. No assertion/guard exemptions. See PhaseD REPORT and UNIT_FIXTURE_CHECKPOINT_38265018734.json.

Latest narrow hosted run38270929907 (abc3b33):371 PASS/7 FAIL/0 SKIP across378 unit/component testcases; earlier362/15 checkpoint and57 gate/108 acceptance preflight retained as history. Confirmed fixture corrections preserve actual application transitions and strict guards; native forwarding acceptance is separate. Seven remaining apply/bypass fixture paths under targeted correction; full26+11+1 bundle and affected reproducibility remain OPEN. PhaseD BLOCKED; no production/PhaseE/CD/L7 changes.

Latest residual unit acceptance38291091850 (37ae2b6):149 PASS/0 FAIL/0 SKIP; preflight57/108/41/13 PASS. Known shared fixture/runner clusters are closed only in unit scope. Next mandatory gate: qualified native + fixed-base affected L0-L5 + exact26 ordinary/11 Provider-kernel/1 packet bundle and green-only repeat. PhaseD BLOCKED pending complete current-source acceptance; no L6/L7/production/PhaseE/CD execution.


## Ordinary component prewarm ownership (2026-10-13)

Ordinary backend component fixtures reserve the existing prewarm admission flag before setup and retain it through teardown, preventing unowned asynchronous read-model loading across restored adapter fixtures. Dedicated runtime-prewarm lifecycle tests opt into tracked real Python threads, release Event barriers and bounded-join all workers before fixture restoration/cache clearing. This is component isolation, not application/native prewarm acceptance. Qualified child/native profiles and application Compose services keep real scheduling.

Probe-cache invalidation tests use deterministic Event barriers and an exact hash-pinned prior-source fixture to demonstrate the stale-publication defect. They assert original waiter completion, replacement-entry ownership, matching invalidation, exception propagation and TTL/force-refresh behavior. Execution receipts are required before claiming PASS; no sleeps, retry-to-green, global healthy mocks or production host tests.


## Phase E event and promotion contract (2026-10-13)

Normal feature pushes run quick affected L0–L1. Optional diagnostic jobs require explicit markers and are never the required PR check. PR/main retain the stable `Affected L0-L5 with hosted profiles` job, with fail-closed exact affected selection/native receipt aggregation. Main additionally requires the exact registered native application smoke: reuse a validated selected ordinary26 receipt or run the singleton diagnostic; missing/stale/skip/incomplete cleanup is ineligible. A smoke singleton cannot substitute for selected full partitions. No forced unselected full functional/QCP/Xray repeats solely because push=main.

L6 nightly/manual/release and L7 explicitly acknowledged manual/release remain separate, NOT RUN by PhaseE default. Trusted GHCR publishing is gated by verified main dependency paths, acknowledged main manual event or the explicitly authorized temporary stage marker; only publisher receives packages-write. Read-only consumers use immutable digests and fresh checked-out source overlays, not application code baked into dependencies. Publication does not auto-edit the reviewed consumer digest or deploy. Actual PR/main event validation and required-check/squash settings need authorization and remain distinct from branch-equivalent runs.

## PR8 final mandatory acceptance policy — 2026-10-15

Ordinary feature/PR development retains affected selection. Explicit final trusted-PR `ci:full-acceptance` and every actual main push select `plan --mandatory-all --include-native`: all existing safe primary L1-L4 manifest suites plus L0/static and existing L5 regression aggregation. Immutable source/base, actual changed paths, manifest digest and exact full selection are revalidated; omitted mandatory suites fail closed. This mode is distinct from scheduled/manual L6 and excludes L7/destructive staging. It reuses the existing hosted profiles, stable required checks, digest/source proof and cleanup; no deployment authority is added. PR8 uses targeted identity/startup qualification before its one final full required run. At that historical PR8 checkpoint, the runtime-scale case used45s readiness deadline and recorded40s engineering headroom as an advisory target (user clarification2026-10-15), exact82 synthetic identities, native parity, steady API load and bounded memory/shutdown. Production restoration of the actual78 identities remains a separate future controlled operation. Current hosted status is recorded in the runtime stability report.


## Static analysis foundation — 2026-10-18 contract

The earlier changed-file F821 checkpoint above is historical. The current foundation extends that same owner instead of adding a second CI pipeline: exact Ruff0.17.0, shared root configuration, globally clean full F/E9 rules on maintained tracked Python, and whole-file F/E9 for new/modified Python. Deliberately changed historical scripts are included in strict checks; untracked/ignored evidence is not recursively scanned. New files must be indexed for the local tracked-file command. Staged/unstaged tracked changes are included locally; hosted execution binds the committed source. Inline noqa cannot hide a failing check.

The existing jobs invoke lint immediately after immutable base resolution, before routine/hosted work, while L0 retains its own required evidence. Missing tools, wrong versions, invalid inventory/configuration, truncated output and violations fail nonzero. Required-check identity, strict aggregation, native proof, startup/memory bounds and event policy are unchanged. [Developer commands and limitations](../STATIC_ANALYSIS.md); [baseline, decisions and qualification](../audits/static_analysis_foundation_2026-10-18/REPORT.md).

Historical F401/F841/star-import debt remains visible and unqualified; no growing baseline allowlist or blanket-ignore grants new code an exception. Broad style/formatting and strict backend-wide typing are not required by this foundation: the measured typing debt requires a separate scoped contract/leaf pilot, not bulk Any/ignore_missing_imports. Static PASS is not native/recovery/production acceptance.

## Ruff debt completion and matched startup diagnostics

The [completion report](../audits/ruff_completion_startup_2026-10-18/REPORT.md) supersedes the former nonzero maintained lint baseline without rewriting earlier receipts. Global F/E9 is enforced by the existing gate with real negative canaries. The temporary Phase D startup-comparison marker is restricted to its reviewed branch and runs nine balanced, immutable-source attempts through the existing qualified launcher. Individual startup/native/resource checks stay unchanged; the larger comparison-only job envelope is for sequential attempts, not retries or a relaxed startup gate. Diagnostic evidence does not substitute for final mandatory PR CI.


## Qualified API startup/readiness policy — 2026-10-21

The user approved a60-second hard deadline for full startup/recovery API workers, replacing the previous45-second ceiling. Healthy readiness observed at or below45seconds is PASS; above45and at or below60seconds is PASS plus `SLOW_STARTUP` with actual duration and owned-worker resource evidence. Unready after60seconds is TIMEOUT/FAIL with retained phase/stack/resource/cleanup diagnostics. The clock begins at process spawn and is not reset by probes, warnings or recovery. Diagnostics/disposal may finish after the deadline but cannot extend readiness or produce PASS. The40-second engineering target remains a separate advisory metric.

Readiness requires the existing healthy service/database/schema response and a still-live worker; recovery-enabled qualified runs must also confirm completed successful bootstrap/recovery. The longer deadline never permits an unhealthy HTTP200, failed worker/recovery, missing native proof, stale publication or integrity violation. Other native/packet/identity/fencing/recovery assertions remain unchanged. Minimal fixtures with full startup disabled retain their separate15-second bound; that is not a second deadline for full startup.

The parent launcher and packet/scale suite bounds remain900seconds, covering multiple startup waits and existing bounded diagnostics/cleanup. Source API systemd `TimeoutStartSec=120` and `TimeoutStopSec=20` are unchanged; `Type=simple` service activation does not itself prove HTTP readiness. Acceptance Compose has no healthcheck start-period/retry gate that truncates this wait. Gateway source TCP wait is already60seconds and is not a healthy API proof. Controlled lifecycle resume uses an8second per-request health precondition, not a startup wait. The older standalone `post-code-reconcile.sh` has a separate configurable15second maintenance wait, is outside the qualified hosted/controlled deployment path and is not qualified by this decision. Installed services were not inspected or changed. Immutable historical startup-comparison experiments retain their original45-second contract and are explicitly excluded from current-policy qualification.

Only the bounded1Hz/64-entry startup resource ring remains in the ordinary scale test, integrated with the existing sampler and frozen/exported at PASS or FAIL. The one-shot paired workflow/driver, CPU calibration and global JSON/SQLite/cache recorder are removed. Measurement uses the owned worker and visible acceptance cgroup; unavailable counters stay null, scheduling scope and observer cost are labelled. No permanent profiler is introduced. The historical D-12 failures and paired measurements remain in the audit: PARTIALLY EXPLAINED, original variability cause UNKNOWN. A new final source-bound mandatory CI is required for this changed policy; no speedup or production acceptance is inferred.
