# Stage 6.1 — Final Backend Finding Dispositions

Review baseline: `a126f3c6b54a170ab7f6bf8292f8cf4b77ba5abb` in
`/root/fwrouter-approved-main`, branch `stage6/backend-completion`.
The original twelve-finding audit inspected `f559a27d5efe745ef1098400c5722c06fd03af36`
on 2026-11-02. Its owner-local storage under
`/srv/fwrouter/knowledge/audits/stage6_1_backend_architecture_2026-11-02/` does
not make that older checkout the reviewed source. Original audit statements
remain historical; this document records their bounded current dispositions.

A disposition is established by current owners, callers, behavior and tests;
a successful implementation PR is corroboration, not its sole basis. Test
names below identify verification contracts. Actual execution, source hashes,
QA and exact-head CI results belong to [REPORT.md](REPORT.md). No entry below
establishes a production fact or silently requires later extraction work now.

| Finding | Final source disposition | Current evidence and verification contract | Remaining boundary / owner |
|---|---|---|---|
| **F01 — API caller trust** | **DOCUMENTED; broader authentication/deployment policy DEFERRED** | [Complete endpoint matrix](ENDPOINT_AUTHORITY_MATRIX.md) and JSON enumerate actual HTTP method/path/handler identities and distinct admission owners. `main.py` has a loopback launcher and correlation middleware, without global authentication. Presentation writes, secure events, import capability and public subscription paths have separate contracts. Route-inventory/admission tests, effective dependency graphs, reviewed classification/whole-source anchors and registry/origin/scope/revoke/drain suites verify those boundaries, with manual policy review still required. | Trusted-local management is a deployment/caller assumption, not peer-UID or whole-API scope enforcement. The retained UI README broad-proxy example is explicitly bounded as historical, requiring separate trusted-management access control; no current deployed reverse-proxy exposure is established here. Public management/global authentication requires separately approved security policy; do not label the residual eliminated. |
| **F02 — historical 11→12 interruption window** | **RETIRED BY OWNER SUPPORT POLICY; no repair claim** | Package E removed `db/migrations.py` and all historical DDL transitions. `connection.initialize_database` creates only an absent canonical schema-27 file; existing files undergo read-only full initialization admission. The CLI rejects historical apply requests. Current-schema lifecycle tests reject actual old shapes and preserve their bytes/rows. Source consumer search finds no executable migration import/runner. | An interrupted or pre-27 database is not repaired, converted or discarded. Unsupported recovery needs its own reviewed plan. Two exact existing legacy schema-27 profiles remain supported; they are not permission to replay retired migrations. |
| **F03 — reciprocal Apply/Mihomo façades** | **RESOLVED for the two identified eager cycles** | `apply_orchestrator_public` uses downward jobs/result/drift/service dependencies; handlers use explicit internal owner bindings. `mihomo_reconcile` depends on builder/serialization/validation leaves instead of importing `mihomo_config`. Compatibility exports and consumer-local patch aliases remain deliberate. `installer/tests/test_backend_module_contracts.py` exercises both cold import orders, identity and no-DB/no-thread side effects; Apply/Mihomo behavior tests retain mutation contracts. | Deferred imports, callbacks and module-attribute compatibility bridges still couple domains. This is not a claim of a globally enforced acyclic architecture or deletion permission for private exports. |
| **F04 — Xray coordinator coupling** | **PARTIALLY ADDRESSED; coherent extraction complete within approved scope** | Checkpoint/config-proof/fingerprint leaves and profile planning, observation, views and staging components now have explicit responsibilities. The coordinator delegates these mechanics while retaining writer exclusion, credentials/identity mutation, phase/checkpoint IO, CAS/fences, native publication, settlement and forward recovery. `test_xray_generation_contracts`, planning/observation/recovery suites and cold component imports verify those seams and source-fingerprint coverage. | The coordinator remains substantial and mutable phase orchestration is intentionally not split further. Complete physical modularization or a generic recovery framework is neither implemented nor required by this closure. Further decomposition belongs to a separately bounded change. |
| **F05 — DB/read/cache ownership** | **RESOLVED for identified seams** | `db/connection.py` has no service-cache import. `database_schema_read_model.py` owns keyed schema observation caching. Explicit read-only scopes/openers are used for selected inspection/export/summary paths; normal writers retain WAL, permissions, foreign keys, busy handling and transaction ownership. Lifecycle, events, diagnostics, backup, transfer and offline-admission tests cover missing/corrupt/drift/current states and forbidden writes. | Generic writer sessions remain write-capable by design; a SELECT is not automatically an inspection contract. Read-only SQLite may still use WAL/SHM bookkeeping. Structural admission caching is not row-integrity or native proof. |
| **F06 — ordinary event projection in HTTP route** | **RESOLVED** | `ordinary_event_read_model.py` owns bounded label queries and summary shaping; `routes/events.py` delegates. Existing 1,000-ID/400-member-pair bounds, safe labels, fallback, ordering and response shape remain. Events model/API/visibility tests exercise actual projections; secure events retain their separate admitted serializer. | No consolidation of ordinary and secure disclosure policies, no second event writer and no change to audit transaction ownership. |
| **F07 — missing safe CLI proof reason** | **RESOLVED** | `_run_legacy_inventory_admission` emits `native_proof_reason` only on failure, only for strings in the proof owner's allowlist; other safe keys, return codes and success shape remain. Actual CLI tests cover valid, unsafe/malformed, oversized, missing and exception cases. | No arbitrary producer payload/exception dump and no generic error framework. CLI behavior is source-qualified, not a claim about a live failed generation. |
| **F08 — repeated worker bookkeeping** | **RESOLVED within scheduler lifetime scope** | Seven scheduler facades own independent `WorkerLifecycle` instances. Twenty-three prior policy functions are AST-identical after stop-Event reference normalization. Tests cover concurrent starts, repeated start/stop, retained live handle and set cancellation after timeout, restart refusal and partial start failure. First-tick/interval/admission/logging policy stays with each actor. | Cooperative cancellation remains cooperative; the shared shutdown budget is not force cancellation. Worker targets must not acquire lifecycle-owner locks. Prewarm retains its distinct owner. |
| **F09 — implicit process composition** | **RESOLVED for supported single-process model** | `ApplicationLifecycle` coordinates explicit narrow operations, factory evaluation occurs inside the fixed host lease, and JobManager owns once-registration completion/lock. Repeated-app lifecycle tests, startup failure/admission and drain/prewarm suites verify behavior. Settings/path defaults and process-owned service façades are retained and documented. The qualified Package G follow-up removes only the stateful settings-workspace projection from incidental prewarm after proving a real module-sync/generation-snapshot race. Real DB/fingerprint/guard regression preserves explicit UI/status invalidation and fail-closed rejection. | `create_app` does not isolate route/service state. Concurrent active lifespans or environment/state-root switching during work are unsupported. This is not a generic dependency-injection rewrite or a promise to eliminate all module state. |
| **F10 — cache recursion/follower wait** | **PARTIALLY ADDRESSED; remaining wait policy explicit** | Caller audit found no built-in recursive same-key chain; raw adapter/loader reentry is guarded by per-flight owner identity. Controlled tests prove fail-fast recursion, real coalescing, BaseException propagation, retry and clear-generation identity fencing. Completed cache bounds/TTL/native freshness remain unchanged. | A custom/nonreturning leader can still retain followers. TTL is freshness, not an I/O deadline; no universal supported caller budget justified a new timeout. Caller/loader deadline responsibility and cross-thread/custom-loader dependency risks remain; no stale proof fallback. |
| **F11 — stale architecture/roadmap status** | **RESOLVED for current navigation and affected architecture** | Knowledge/project-map entry points now identify canonical authority, current backend owners and dated historical snapshots. Architecture explicitly separates current implementation from target extraction; old Compose wording is retained as historical and the latest planned Stage 6A scope is identified. This closure/report and Code Index connect all findings to current evidence. | `knowledge/ROADMAP.md` is deliberately preserved, including its pre-existing unpublished change. Canonical Stage 6A decisions/history are not overwritten or represented as published/implemented. Historic production receipts are not fresh live observations. |
| **F12 — Gateway HTTP join gap** | **RESOLVED for bounded HTTP contract** | `installer/tests/test_xray_sub_gateway_http.py` runs the real Gateway Handler against an ephemeral loopback upstream. It verifies GET/HEAD wire behavior (including a negative HEAD-body detector), header/path/query/status/body forwarding, invalid paths without upstream calls, timeout/refusal/disconnect/malformed errors and log redaction. Backend gateway helper tests remain. | No systemd/NPM/production transport or real external-client VPN acceptance follows. Gateway remains a specialized separately deployed component. |

## Combined E+F architecture and persistence review

- Current AST inspection covers 281 non-test `fwrouter_api` modules. It resolves
  relative/internal imports, excludes TYPE_CHECKING-only edges and distinguishes
  syntactic eager imports from function-deferred imports. Result: 1,208 eager
  edges and **zero multi-module eager SCCs**. The potential graph including
  deferred imports has 1,592 edges and SCC sizes 136 and 2. Dynamic imports,
  callbacks, module execution and runtime concurrency are not proved by this
  static graph. Cold-import and behavior tests are separate checks.
- `db/schema.sql` remains byte-identical to the approved schema-27 baseline
  (`c48686ecde23308ca397a69a4ea3e67de7e2d86697fd6aee74c40cd9f9bf26c6`).
  Initialization is called by explicit bootstrap and private rebuild-candidate
  construction, not health/inspection. Existing-file validation precedes
  startup-dependent work and import publication. Writers cannot create a
  missing database; structural admission is on the same SQLite handle before
  write-capable permission/WAL setup. Domain services still own transactions.
- Fresh schema is canonical-only. Existing admission retains exactly
  `legacy_migrated_schema27_from_7` and
  `legacy_migrated_schema27_from_10_or_11` profiles from Package E. Both retain
  the historical absence of three routing-global-state FKs; that is an explicit
  compatibility limitation, not canonical FK equivalence. Unknown hybrids,
  unsupported versions and corrupt/partial states fail closed.
- WAL-aware SQLite backup captures and compares a pinned source snapshot;
  backup/source validators retain the exact supported profiles. Private rebuild
  candidates remain canonical-only, and exact source→candidate object equality
  refuses an unsupported legacy-shape rebuild rather than transforming it.
  Import validates structure/integrity/FKs before transactions and file/receipt
  preparation. Offline maintenance/source/host guards and the installer obsolete
  migration-file removal list remain owned by their established components.
- Package F does not move durable intent into workers/cache, change Core or
  selection revisions, or transfer checkpoint/native publication authority.
  Lifecycle shutdown first signals schedulers then drains joins/prewarm/jobs
  under the same cooperative budget and fixed lease. Timeout retains live
  handles; cache clear fences old flights; mutation-owned invalidation remains.
- Qualified native/process/packet/recovery and actual Compose database
  preservation remain required existing branch gates. Synthetic tests and
  source equivalence do not replace their results or external live acceptance.

## Readiness interpretation

The remaining F01, F04 and F10 boundaries above are explicit policy/technical
residuals, not undisclosed exemptions or proof that the original twelve findings
are all erased. No known unaddressed in-scope implementation defect may be
classified closed merely to produce a READY verdict. Package G source readiness
also requires its actual regression, independent QA and exact-head branch CI
receipts. Opening the single E+F+G PR is a later owner instruction; PR/main
acceptance and production deployment/acceptance remain distinct unrun gates.
