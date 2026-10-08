# Phase C Xray application/native acceptance extension

Date: 2026-10-08
Baseline requested: `c25ef4c` on `stage/test-architecture-ci-stabilization`
Scope: `tests/application_acceptance` Xray worker, native runner, fixtures, and Xray scenarios only.

## Evidence boundary

This report describes source coverage, not runtime acceptance. The hosted native profile was not available to this agent, and no application worker, Xray/Mihomo process, browser, Compose stack, or crash test was run. Validation performed here was static Python AST parsing only. The scenario assertions below are intended to run only inside the fixed qualified hosted profile; all test APIs and processes remain loopback/owned by that fixture.

## Xray scenarios added or extended

| Scenario | Contract exercised | Source depth and assertion |
|---|---|---|
| Email client generation | Real API request, job persistence, profile snapshot, generated config, restart | Email creation goes through the public Xray client API. It checks the persisted account/client/snapshot rows, generated UUID/email identities against active config and actual Xray HandlerService users, exact native mounted config archive parity, idempotent repeat request, and worker restart plus public reload. |
| Email generation reload failure | Desired/applied distinction under failure | One-shot fault is at the real native reload transport. The test requires the job to fail, the desired config to retain the attempted email identity, and actual HandlerService users to remain exactly at the prior loaded set. This follows the checked-in adapter contract that saves desired bytes even when reload fails; it does not claim desired-file rollback. |
| Worker crash during native reload | Actual process SIGKILL/restart | Existing scenario pauses the owned Xray child at its real reload RPC, kills the API worker, restarts it, and checks native process and config parity. It does not claim checkpoint-recovery coverage. |
| Generation checkpoint crash matrix | Eight literal durable checkpoint phases | A real email account is first created through the public API. The test configures a real synthetic provider through the API and starts its ordinary `enable` operation, whose normal subscription pipeline invokes Core selection and the actual prepublication callback. The fixture blocks only after each requested checkpoint directory fsync has completed. The phase assertion proves the requested durable checkpoint was reached before SIGKILL. Recovery invokes the normal reconciliation service and must succeed, remove the checkpoint, preserve linked account/client/snapshot rows, match generated identities against actual active config, applied binding projection and HandlerService users, match the actual mounted config archive byte-for-byte, and make repeated reconcile a no-reload byte-identical idempotent operation. |
| Newer Core intent at checkpoint | Source fingerprint and stale checkpoint rejection | A real provider enable generation is held at its durable `prepared` checkpoint. The test changes global routing mode through the public Core API to `direct`, waits for the real intent job, then kills/restarts the worker. Recovery must return explicit failure for `generation_source_changed`; the public routing readback must still report the newer `direct` intent. |
| Newer Xray source rows after checkpoint | Generation source change and no identity resurrection | A real provider enable generation is held at `prepared`, then the worker is killed. After restart, a public Xray client delete changes the canonical source rows. Recovery may either fully reconcile the newer source or decline with the exact `generation_source_changed` typed failure; either way the deleted email must remain absent from the active config and actual HandlerService, with active/native parity. |
| Concurrent Xray CRUD | Writer serialization and committed binding preservation | With an actual create paused at native reload, public alias update and delete requests race through the API. After release the test requires the original identity with updated alias and applied binding, the deleted new identity absent from native HandlerService, and exact active/native parity. |
| Xray process incarnation race | Captured generation fence and runtime readback | A real provider enable generation is held after its durable `xray_applied` checkpoint, whose actual `xray_runtime_incarnation_after` and owned PID are recorded. The fixture restarts the actual Xray process from active config before releasing the barrier. The test requires job failure containing exact `XRAY_GENERATION_STALE_BEFORE_PUBLICATION` and compares active/native mounted/HandlerService parity. |
| Xray pre-success readback barrier | No false terminal success and duplicate-create coalescing | The test holds the actual HandlerService user readback after a public email create, reads the persisted job through the public jobs API while the native read is blocked, and asserts it is not terminal. A duplicate create for the same email must resolve to the same active job. After releasing the native boundary, it waits on the worker's bounded job wait-control route and requires exact native identity/config parity. |
| Xray readback transport failure | Explicit readback failure | One actual `api_inbound_users` RPC is failed at the transport boundary. The API job must terminate as failed and the test compares the active config against the actual mounted config and HandlerService identities; no synthetic loaded-client result is returned. |
| Forced public reload failure | Explicit rollback/readback | A fault is injected at actual Xray reload transport and the public reload API must preserve the active file and actual loaded identities. |
| Native candidate rejection | Pre-promotion validation | Existing `test_xray_api.py` corrupts only the owned Xray candidate artifact, then requires candidate validation failure to preserve active config and actual loaded identities. |

Checkpoint phase IDs are literal and stable for the offline scenario catalog: `checkpoint-prepared`, `mihomo-transition-applied`, `xray-applied`, `runtime-applied`, `inventory-persisted`, `bindings-persisted`, `projections-cleaned`, and `selection-verified`. The matrix reaches those phases through the normal provider enable workflow, so `selection_verified` is written only after the actual pipeline callback performs Core selection and provider handoff readback. Recovery does not echo a requested revision as observed proof.

## Harness seams

`worker.py` only enables the acceptance routes with the fixed qualified test root and test environment. The Xray barrier wraps the directory-fsync boundary and calls the original fsync before notifying the parent. It reads the real generation checkpoint file to identify its durable phase. The Xray API, generation, checkpoint, and recovery functions remain real.

The test runner may pause/fail actual Xray RPC transport operations and restart its owned native process. Its bounded RPC listener allows up to eight concurrent connection threads so a held crash/probe barrier does not prevent independent owned-process inspection; teardown releases and joins every owned thread. Each Mihomo launch copies the requested active config into an immutable private launch snapshot and starts the actual child from that snapshot; tests compare those exact launch bytes with the active generated file. The Mihomo test adapter retains real loopback HTTP controller requests and substitutes only the unavailable Docker process transport with RPC operations on its owned child. Its acceptance subclass redirects only Mihomo's requested external probe URL to the provider bridge's `/generate_204`; actual Mihomo timing/error responses are still read over controller HTTP. A post-probe barrier pauses only after that actual HTTP response, and never replaces its value. The provider adapter retains the real HTTP client and parses responses from a loopback-only synthetic server. The Xray fixture includes a separate fixed VLESS TCP inbound used only as the provider test upstream; application client identities remain scoped to the managed `vless-ws` inbound.

## Final source review and execution boundaries

The precise CAS scenario is now implemented in
`test_real_core_selection_cas_miss_reconciles_after_native_readback_without_provider_retry`.
The real provider-enable application path reaches original Core
`commit_active_selection` after actual native apply/readback. A test-only RPC
barrier pauses before the **original** CAS; an explicit qualified-worker fault
route advances the actual owned SQLite monotonic revision without changing
active/provenance. This is deliberate persistent-state fault injection, not a
claim that a legitimate competing production writer exists. The original CAS
must miss; real Core fresh reconciliation must repair convergence without a
second external request or provider retry, with selected outcome only after
proof. A public-API competing selector test and the held-checkpoint newer-Core
intent test separately cover legitimate competing intent and no overwrite.

Source fingerprint changes are covered by both newer Core routing intent and
newer canonical Xray deletion. The eight-phase crash matrix checks partial
application/persistence and complete recovery; generic partial/failed status
is not a PASS escape. A separate provider-specific duplicate fingerprint test
is not required to re-test the same shared generation ownership contract.

All native/application/browser/crash scenarios are **NOT RUN — pending Phase D**.
Actual stock Docker/systemd/kernel/reboot/runtime-bootstrap parity remains
outside this native-process profile. Fault outcomes and field assumptions must
be checked by remote execution; static source review does not establish PASS.
Seven historical failures remain open, and the historical overmocked fixture
cannot be retired until actual replacement acceptance succeeds.
