# Phase C — Missing Test Coverage: source completion

Updated: 2026-10-09. Requested checkpoint: `c25ef4c`.
Branch: `stage/test-architecture-ci-stabilization`.

## Verdict

**SOURCE COMPLETE / EXECUTION PENDING.** The obligatory gaps in the checkpoint
report now have concrete application/native/browser/recovery test sources and
exact registered IDs. This is NOT full application acceptance. All 48 hosted
cases are **NOT RUN — pending Phase D**. Phase D/E, workflows, CD, deployment,
production runtime changes and remote execution were not performed.
Operational Performance Fixes remain PAUSED/BLOCKED at unchanged `6004400`.

The original checkpoint report/matrices/evidence are retained as
`CHECKPOINT_C25EF4C_*`; see [historical report](CHECKPOINT_C25EF4C_REPORT.md).
The Phase A full inventory remains authoritative for historical test quality;
the updated inventory here is the acceptance/gate delta, not a replacement.

## Source coverage and depth

31 application test definitions expand to **48 exact cases**: 37 functional L3
and 11 explicit L7 recovery cases. Compared with checkpoint's five cases, this
adds **26 definitions / 43 cases**, plus substantive extensions of the existing
browser and worker-crash definitions. Three new pure gate/catalog contract
checks supplement the existing tests. Manifest: 158 classified test files.

| Mandatory invariant | Concrete source | Proof required by assertions |
| --- | --- | --- |
| External Xray CRUD and persistence | `test_xray_api.py`, `test_xray_generation.py` | Public HTTP/API/jobs; create/alias update/delete; restart; linked account/client/profile snapshot; native HandlerService identities; existing bindings preserved |
| Generation/validation/apply/native parity | Xray generation + candidate rejection | Real service generation, pinned native validator, actual process reload, generated/active/immutable launch bytes, loaded users and applied bindings |
| Partial application and crash recovery | Eight parametrized durable checkpoint phases in `test_xray_worker_crash_l7.py` | Original directory fsync completes before pause/SIGKILL; real reconcile must succeed and consume checkpoint; repeated reconcile must not reload unchanged bytes |
| Newer intent/source rows | Newer Core Direct intent and canonical Xray deletion at checkpoint | Exact typed stale-source refusal or complete newer-source reconcile; no resurrection/overwrite; real native proof |
| Incarnation and revision fences | Xray process replacement; Core/provider selector and exclusive races | Actual native replacement / public newer intent; old evidence cannot publish or clear Direct |
| CAS after successful readback | `test_real_core_selection_cas_miss_reconciles_after_native_readback_without_provider_retry` | Original Core CAS misses after real revision fault; fresh real reconcile repairs without second external call/provider retry; outcome selected only after convergence |
| No early success / failures | Held HandlerService readback, transport/reload/validation faults | Job nonterminal while readback blocked; faults produce failure, actual loaded last-good survives; desired/applied distinction remains explicit |
| Core/exclusive/Provider/Mihomo | `test_core_provider_mihomo.py` | Real Core jobs, saved source, real provider HTTP client, native Mihomo controller and immutable config snapshot; exclusive intent and active target consistent |
| Provider unavailable is not DOWN | Discovery faults AND four joined phase-2 recovery fault cases | Timeout/429/503/malformed produce exact unknown/evidence codes, no DOWN, discovery or PATCH; last-good binding retained |
| Emergency Direct/reentry | Confirmations, failed/successful reentry, two reentry races | Real effective Direct apply; same-member verified return over native VLESS→Xray→HTTP path; failed/stale evidence retains Direct; policy-suppressed API calls absent |
| SQLite migrations/rollback | `test_database_application.py` | Real v23-shaped provider table upgraded by product initializer; secrets/bindings preserved; FK/integrity; aborted real transaction invisible after restart |
| Browser→API→state | Four real Chromium scenarios | RU/EN, 1440/390, navigation/overflow/JS errors, Settings persistence, loading/refusal/validation states, no false success, ordinary configured Auto editable under Provider exclusive, Xray API/job/native effects |

Exact invariant→node→depth→environment→gate→status→PASS criterion rows are in
[TRACEABILITY_MATRIX.csv](TRACEABILITY_MATRIX.csv) and
[COVERAGE_MATRIX.csv](COVERAGE_MATRIX.csv). Full case inventory is available as
[CSV](TEST_INVENTORY.csv) / [JSON](TEST_INVENTORY.json).
See [Xray details](XRAY_EXTENSION_REPORT.md) and
[joined/browser details](JOINED_EXTENSION_REPORT.md).

## Isolation and fidelity

The existing hosted-only launcher checks observed host layout, clean tracked
source, pinned inputs and rendered/inspected Compose resources before execution.
One UID10001 readonly container, internal network, no published ports, Docker
socket, host networking/devices/capabilities, production mounts, secrets or
provider egress. CPU/RAM/PID/tmpfs limits and exact owned cleanup remain enforced.
Hosted labels are not cryptographic attestation; actual qualification is pending.

Application generation, Core selector, jobs, SQLite, recovery and readback stay
real. Adapter seams replace Docker process transport with pinned owned native
children and provider upstream with a synthetic loopback HTTP server. Probe
responses are produced by the actual Mihomo→native Xray→local HTTP path, never
fabricated latency/health. RPC is bounded to eight connections; held barriers
permit independent native inspection; teardown verifies threads/processes/socket.
Mihomo/Xray parity reads immutable actual launch snapshots, not mutable output.

CAS conflict uses an explicit test-only route invoking the real monotonic
SQLite revision helper immediately before original Core CAS. It is controlled
state fault injection, not evidence of a production competing writer. Separate
public-API races cover legitimate competing intent. No service result/CAS return
is mocked into success. Crash barriers call original fsync before pausing.
Timeout/loading barriers are bounded events, not sleep-based success assumptions.

## Source review corrections

Review fixed missing imports/job-wait references, wrong exclusive eligibility
assertions, a sequential RPC barrier deadlock, mutable-file parity tautology,
no-op generation triggers that could never reach checkpoints, and broad failure
assertions; successful synthetic provider PATCH now persists its result for subsequent real HTTP confirmation. Failed reload tests now honor the existing save-desired-on-failure
contract (`backend/tests/test_xray.py::test_reload_failure_after_config_save_does_not_rollback`):
desired bytes may retain pending intent while actual native users remain last-good.
Failure does not require false desired/applied parity or silently discard intent.
Unsupported static collection forms and stale scenario registries fail closed.

## Actually executed checks

- L0: syntax/AST, JSON/TOML, manifest/source-registry/traceability/static scope and
  diff checks only. No app import or application/native execution.
- L1/L2: **60/60 pure gate, acceptance and smoke-contract checks PASS**. Smoke
  contract tests use synthetic command/HTTP boundaries; this is not deployed or
  real application L4 smoke. [Initial bounded cohort](LOCAL_EXTENSION_EVIDENCE.json);
  [final evidence](FINAL_LOCAL_EVIDENCE.json): 60/60 in 3.107s
  (3.240s wall), CPU 1.003s user / 0.292s system, RSS 37,012 KiB.
  Final L0 parsed 19 Python files and validated all 48 traceability IDs.
- L3 native/application/browser: NOT RUN. L4 actual runtime smoke: NOT RUN.
  L5 application/domain regression, L6 full suite and L7 execution: NOT RUN.

The pure cohort took 3.104s (3.254s subprocess wall), 0.939s user CPU / 0.299s
system CPU, maximum child high-water RSS 36,764 KiB. These are local harness
contract costs, not acceptance/runtime performance or aggregate concurrent RAM.
Native suite CPU/RAM/I/O/temp usage and reproducibility remain unmeasured.
An interim 47-test pure run failed during source integration (stale registry and
new required-suite policy oracle); those harness issues were corrected before
final PASS. No result is relabeled as a product/baseline regression.

## Historical failures and risks

Seven historical failures remain OPEN: five watchdog_state lazy read-path writes,
one topology oracle/contract discrepancy, one overmocked Xray lifecycle fixture.
None was hidden, skipped, allowlisted or weakened. The new native replacement is
source only and cannot retire the historical fixture failure until actual runs.
Four Phase B resolved IDs retain their historical bounded evidence; not rerun here.
Potential product failures discovered by new negative tests must be recorded by
remote execution separately from fixture/provisioning defects; application code
was not changed to obtain PASS.

## Remaining gates

1. Separately authorize Phase D to provision/pin and qualify GitHub-hosted Ubuntu
   inputs; verify real Compose inspection and fail-closed admission.
2. Execute all 37 functional cases twice clean, with exact JUnit/inner/outer
   receipts and no skips; qualify real browser/native libraries and versions.
3. Execute all 11 L7 cases only at the explicit recovery/release gate, including
   SIGKILL/partial persistence; prove orphan-free cleanup and resource bounds.
4. Validate actual field/phase assumptions, fault attribution, no false success
   and reproducibility. Missing evidence/failure blocks full application acceptance.
5. Stock Docker-adapter parity, systemd/startup tasks, nftables/kernel/reboot and
   external Internet handshakes are outside this native-process profile. Record
   incompatible hosted scenarios as explicit unverified gates, not mock PASS.
6. Seven historical failures and Phase D/E acceptance still block overall CI
   milestone closure and return/merge of Operational Performance Fixes.

Run contract: [hosted instructions](/srv/fwrouter/tests/acceptance/RUNNING_ON_GITHUB_HOSTED.md).
Source completion does not authorize Phase D, push/PR/merge or deploy. Stop for review.
