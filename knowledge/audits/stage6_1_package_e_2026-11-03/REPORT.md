# Stage 6.1 Package E — Schema Lifecycle Retirement

Status: **implementation acceptance VERIFIED at `2b79fab74c05e50342c793a2a69b0cda6f3e7479` (source, independent QA, exact-head branch CI and offline bundle). Final documentation HEAD is qualified separately and recorded in the canonical roadmap. Not merged or deployed.**

## Source identity and basis

- Repository baseline: `f7847a39ed89ea34a4f0a5741f1be5f4e4143354` (2026-11-03).
- Final candidate file digests for this Package E work are recorded in [SOURCE_SHA256.txt](SOURCE_SHA256.txt), SHA-256 `0c0c00425f880fd49ba2c5d81f74c87b40a0e382502520b86a85568ebb3f6594`. The manifest covers 42 changed Package E paths, excludes this report and the Orchestrator-owned shared roadmap pointer, and records deleted historical implementation files explicitly. This is a local source candidate, not a commit or publication.
- Historical `backend/fwrouter_api/db/migrations.py` SHA-256 at baseline: `21de626f75bfa3531fb9f19998cbf22788553e9b3bb94ce94acb7cf7ab74ea39`.
- Canonical `backend/fwrouter_api/db/schema.sql` SHA-256 at baseline: `c48686ecde23308ca397a69a4ea3e67de7e2d86697fd6aee74c40cd9f9bf26c6`.
- Baseline fresh-schema reference generated independently from that exact `schema.sql`, without importing the application: [schema-baseline.json](schema-baseline.json), SHA-256 `ce8733db63fc5c94d2d49822dda8acf260eab9ddf674916d40b7047d0ad6011a`. It records 46 FWRouter tables plus SQLite's internal `sqlite_sequence` table (47 total), 129 SQLite objects, `integrity_check=ok`, and no foreign-key violations. `CURRENT_TIMESTAMP` seed values are nondeterministic and are compared semantically, not byte-for-byte.
- The full historical source remains retrievable from Git at the baseline commit; this note records its registered transitions and retirement disposition.

## Support policy

Current supported schema is exactly **27**. Existing databases at versions below 27, above 27, missing metadata, or with unknown/corrupt structure are rejected intact. In addition to canonical schema 27, existing-file inspection retains two exact, baseline-derived structural profiles emitted by previously supported 7→27 and 10/11→27 migration routes. These profiles preserve complete object inventory and canonical semantics everywhere except their exact known table fingerprints; they do not run migrations or change the files. Both profiles lack the three `routing_global_state` foreign keys to `servers`; the v7 profile also retains its historical inline CHECK and ALTER-added column declarations. No general missing-constraint allowance is introduced. Fresh initialization, verified-current CLI, and private rebuild candidates remain canonical-only. Do not infer that retirement repairs a partially applied historical transition. In particular, historical `11→12` was not wrapped in the later 25→26 savepoint boundary; interruption recovery for such a file remains a separate owner-reviewed recovery decision.

Only explicit bootstrap initialization and an admitted private rebuild candidate may create a database, and only when the target path is absent. Fresh initialization compiles canonical `schema.sql` into a private sibling, validates the complete canonical schema, integrity, and foreign keys, and publishes without replacing a concurrent file. A concurrent winner is admitted read-only with full structural/integrity/FK validation and is never overwritten or repaired. Existing schema-27 initialization is observational and never reseeds deleted defaults. Normal writers open with `mode=rw`, validate schema on the same SQLite handle, then perform established permission/WAL setup; ordinary opens do not run whole-file integrity checks. Read observations use `mode=ro` and do not create missing paths.

The full validator compares the canonical SQLite object inventory and semantic table columns, defaults, PKs, FK order/actions, checks, and index key order. The old CLI admission explicitly allowed additive non-unique performance indexes; that narrow behavior remains. Added unique indexes and extra tables/views/triggers, missing canonical objects, or modified canonical constraints are rejected unless the entire exact legacy-profile mismatch set matches. The retained `external_connection_migrations` table records domain-data migration markers and is not the retired schema DDL runner.

Structural validation uses a process-local positive LRU capped at 32 entries. Each new connection reads the current `schema_meta.schema_version` and hashes ordered same-connection `sqlite_master(type,name,tbl_name,sql)` rows inside a query-only read transaction. The key also includes the canonical `schema.sql` and SQLite runtime identity. Only a successful full admission is cached; invalid schema/version results are not. DDL/content changes produce a new key, and restart discards the cache. This cache proves structural admission only, never row integrity, persistent intent, or native evidence. Explicit initialization always performs full structure, `integrity_check`, and `foreign_key_check` validation.

`fwrouter database upgrade --expected-schema 27` remains an integrity/schema-verified, canonical-only non-mutating already-current check. Historical apply requests (including 24/25/26→27) return `DATABASE_SCHEMA_MIGRATION_RETIRED` without opening or modifying the database. Recovery or data conversion for a legacy DB requires a separate explicit reviewed plan.

### Existing legacy schema-27 profile evidence

Profiles were measured from disposable tests at baseline commit `f7847a39ed89ea34a4f0a5741f1be5f4e4143354`, using historical migration source SHA-256 `21de626f75bfa3531fb9f19998cbf22788553e9b3bb94ce94acb7cf7ab74ea39` and positive cases in `backend/tests/test_db_migrations.py` SHA-256 `acfb3973c86fd2079de6aa1adfabe6d6f89336b5e978ea3c23f52276e86b1ec2`. Baseline v7→27, v10→27, and v11→27 fixtures each passed `integrity_check` with zero FK-check rows; the current exact-profile inspector admitted them as follows:

| Baseline route | Matched profile | Exact table fingerprint set | Retained structural difference |
|---|---|---|---|
| 7→27 | `legacy_migrated_schema27_from_7` | `modules`, `routing_global_state`, `server_custom_https_proxy`, `server_preferences` | Three absent routing FKs; column-inline CHECK placement and ALTER-added columns. |
| 10→27 | `legacy_migrated_schema27_from_10_or_11` | `routing_global_state`, `server_preferences` | Three absent routing FKs; inline priority-origin CHECK. |
| 11→27 | `legacy_migrated_schema27_from_10_or_11` | Same exact set and fingerprints as 10→27 | Same known routing-FK omission and inline CHECK representation. |

Disposable baseline CLI apply tests for 24→27, 25→27, and 26→27 produced canonical schema-27 structure (`problems=[]`, integrity `ok`, zero FK-check rows). The baseline database-admin schema-10 and schema-11 upgrade fixtures also produced canonical current structure. Other recorded positive migration fixtures included schema-10 bootstrap/idempotence and repeated schema-11 startup, which deduplicate to the profiles above. The allowlist is not proof that every possible historic on-disk database matches a supported profile; unknown profiles remain fail-closed and require owner-reviewed recovery. No production/live database was inspected.

## Historical transition inventory

The baseline runner registered exactly these adjacent transitions, all retired from current source:

`7→8`, `8→9`, `9→10`, `10→11`, `11→12`, `12→13`, `13→14`, `14→15`, `15→16`, `16→17`, `17→18`, `18→19`, `19→20`, `20→21`, `21→22`, `22→23`, `23→24`, `24→25`, `25→26`, `26→27`.

Historical behaviors preserved in dated architecture and audit notes include server identity/membership remapping; provider/subject backfills; profile snapshots; credential scrubbing; provider bindings and credentials; automatic member-switch policy; subscription node identity tables; the Core intent revision; and typed virtual VPN-auto overrides. Those notes are historical records, not executable upgrade guidance. API-startup JSONL scrubbing remains in `main.py`, and unrelated domain-level migration ledgers remain. The old 20→21 database-row scrub is retired with the schema runner; unsupported pre-27 database files/backups are rejected intact and may contain historical credential material. They require separately reviewed secure recovery and must not be treated as current API state.

## Consumer and route disposition

| Baseline route | Current disposition |
|---|---|
| `db/connection.initialize_database()` called the sequential runner and then replayed `schema.sql` on existing files | Now creates only absent files; existing canonical or exact-profile files are observed and fully validated with no seed replay or repair. |
| `services/bootstrap.bootstrap_backend()` startup initialization | Remains the explicit fresh schema owner; failed full admission now prevents cleanup, schema-dependent writes, startup recovery, and apply/reconcile work. |
| `services/database_admin` rebuild candidate | Retains explicit private-candidate initialization, import, backup, validation, checkpoint, and publication ownership. Existing-source and pinned-backup validators admit known profiles; candidates are explicitly canonical-only. The exact source→candidate object equality guard still refuses a legacy-shape rebuild rather than dropping schema behavior. General restore/rebuild helpers are not removed. Schema inspection no longer initializes a missing live DB. |
| Control-plane transfer plan/source readers | No longer initialize/create the DB; import performs read-only full schema/integrity/FK admission under its existing writer guard before opening a write transaction or preparing files. |
| Transfer import planning and export projection DB reads | These are SELECT-only observation owners and now place their nested DB sessions in the existing context-local read-only scope; export's authorized JSON artifact write remains intact. Snapshot source listing is filesystem-only. |
| Ordinary event label lookup and system-summary schema inspection | Bounded label SELECTs and the route's direct schema inspection use the existing read-only scope; neighboring DB writer services retain normal mode. |
| `fwrouter database upgrade` CLI | Keeps verified schema-27 already-current behavior. Historical apply path and backup/preflight engine for legacy DDL transitions are retired. |
| Installer deploy tree | Adds the removed `fwrouter_api/db/migrations.py` path to the existing obsolete-backend cleanup list so a previous deployed copy cannot remain importable. No live installer was run. |
| `live_probe_cache` read projection | Moved from low-level DB connection into `services/database_schema_read_model.py`; existing 30-second TTL remains service-owned. |
| `provider_configuration` tests importing old schema transition helpers | Replaced with current-schema credential/policy preservation checks; no old migration helper remains callable from tests. |
| Four `test_database_admin.py` cases expected startup to prune/convert/repair schema-8/10/11 structures | Removed as unsupported automatic-upgrade promises. `test_actual_schema11_shape_is_rejected_intact` now verifies an actual legacy-shaped file and its rows remain byte-for-byte intact; schema-version tests cover 8/10/11/24/25/26 refusal. Current-schema provider and connection preservation checks remain. |

## Regression route changes

The phase-D exact-node workflow previously selected the old CLI backup/apply cases and the v24→25 identity migration case. The narrow selected node IDs are replaced with current-schema admission and preservation checks; all other identity/runtime/native proof and failure-path nodes in the step remain unchanged.

| Retired node | Replacement | Purpose |
|---|---|---|
| `test_db_migrations.py::test_database_upgrade_cli_is_explicit_backed_up_and_idempotent` | `test_database_schema_lifecycle.py::test_database_upgrade_cli_verifies_current_schema_without_mutation` | Retains current-schema CLI validation and proves byte-for-byte no mutation. |
| `test_db_migrations.py::test_database_upgrade_cli_refuses_schema_24_structural_drift_without_backup` | `test_database_schema_lifecycle.py::test_database_upgrade_cli_rejects_retired_historical_transitions` | Historical apply request is rejected before DB access. Structural drift remains covered by the full schema admission tests. |
| `test_db_migrations.py::test_v24_to25_adds_identity_tables_without_changing_legacy_rows_or_admitting_downgrade` | `test_database_schema_lifecycle.py::test_schema26_is_rejected_intact_without_rewriting_identity_records` | Old schema is rejected intact; identity row remains unchanged. |
| `test_db_migrations.py::test_subscription_identity_migration_preserves_references_and_membership` | `test_database_schema_lifecycle.py::test_schema27_identity_rows_survive_repeated_initialization` | Existing identity, alias, credential, and revision state survives repeated current-schema initialization. |

The new schema lifecycle tests also cover fresh canonical initialization, missing-path no-create behavior for observation/writer opens, unsupported versions 8/10/11/24/25/26, missing table/index, unexpected trigger, changed CHECK/default/column contract, additive non-unique index compatibility, rejection of additive unique indexes, malformed SQLite files, existing-file FK violation refusal, CLI ancestor-symlink/malformed-argument rejection, signature-cache hit/replacement/invalid-result/bounds/concurrency behavior. The event API suite verifies mode/permissions/journal mode remain unchanged on the changed read-only label and schema-inspection seams. All use isolated test state.

## Retained risks and limits

- An earlier unretained microbenchmark summary measured cold 22.915 ms and current median/p95 1.298/1.460 ms versus baseline 0.812/0.955 ms. It is superseded by the final-candidate raw paired measurement below.
- This package does not convert pre-27 DBs. They remain unchanged and require a separately reviewed recovery plan.
- The old 11→12 interruption state is neither repaired nor asserted safe for automatic retry.
- The two exact existing legacy schema-27 profiles preserve historical routing FKs that are absent from those structures; they do not inherit canonical constraint guarantees. No broader historical inventory is inferred beyond positive baseline fixtures inspected here.
- Fresh `schema.sql` equivalence was independently checked by the Orchestrator against the baseline reference (47 SQLite tables including `sqlite_sequence`, 129 objects, exact table metadata/defaults/PK/FK/indexes and seed semantics, integrity ok, foreign-key check empty, idempotent bytes). The Orchestrator repeated this full equivalence check on the final frozen candidate successfully.
- The baseline exact-node workflow still requires a hosted run at the resulting exact source HEAD; editing its selected test nodes is not evidence that they passed.

## Verification record for this candidate

- Backend focused regression command (run from `backend/`): schema lifecycle, provider configuration, module lifecycle, database admin/backup, bootstrap, event API, and control-plane transfer suites — **PASS: 176 passed**, one existing Starlette/httpx deprecation warning.
- Lifecycle/bootstrap/maintenance-admission targeted run — **PASS: 124 passed**, same existing deprecation warning. It includes race-winner valid/invalid FK admission, bootstrap no-mutation/no-recovery, API lifespan abort, import refusal before transaction/files/receipt, and exact legacy profile controls.
- Historical profile evidence, using the disposable `f7847a39` source export and private test venv: baseline positive schema-7/10/11→27 cases — **PASS: 3 passed**, candidate inspector matched profiles v7 and v10/11; baseline positive CLI 24/25/26→27 cases — **PASS: 3 passed**, candidate inspector found canonical structure; baseline database-admin positive schema-10/11 fixtures — **PASS: 2 passed**, candidate inspector found canonical structure. These are isolated historical source fixtures, not production database checks.
- Synthetic proof/recovery regression command: `test_native_proof.py`, `test_subscription_native_generation.py`, `test_xray_generation_recovery.py`, `test_rollout_native_evidence.py`, `test_proof_only_legacy_admission.py` — **PASS: 277 passed**. Qualified `test_native_proof_native.py` was not run locally.
- Gate contract command with pinned Ruff on PATH, `pytest -q -rs tests/gates/test_acceptance_contract.py tests/gates/test_gate_contract.py` — **PARTIAL: 296 passed, 1 skipped**. Exact skip: `tests/gates/test_gate_contract.py:2167`, `/usr/bin/time is required by the real child runner`. Five existing unknown `pytest.mark.browser` warnings were reported. The first attempt without the pinned Ruff PATH failed one static-canary case; rerunning with `/tmp/fwrouter-final-ci-env/bin` passed.
- Installer path regressions, `installer/tests/test_safe_recovery.py installer/tests/test_backend_module_contracts.py` with `PYTHONPATH=backend` — **PASS: 25 passed**. The first attempt without `PYTHONPATH` failed one test import (`ModuleNotFoundError: fwrouter_api`); corrected environment passed.
- Lifecycle/events/diagnostics command: `tests/test_database_schema_lifecycle.py tests/test_events_api.py tests/test_diagnostics.py` — **PASS: 87 passed**, one existing Starlette/httpx deprecation warning. It asserts public `/health` and diagnostics `_check_database` report `status=ok` while carrying exact compatibility metadata, and verifies failed unsupported/missing/corrupt database checks do not create/rewrite/chmod files or invoke dependent diagnostics.
- During the final correction, the original mismatch diagnostic test first failed because full writer admission prevented raw schema inspection; after the read-only observation seam and early failed-report return were added, `tests/test_diagnostics.py` passed **29 tests** and the unchanged `test_diagnose_database_schema_mismatch_is_failed` passed.
- Pinned Ruff 0.17.0, `--select F,E9` over changed Python source/tests — **PASS after final source changes**.
- `git diff --check` — **PASS after final source changes**.
- At this initial source-candidate checkpoint, no full CI, qualified native runtime, deployment, service restart, or production-state check had been run.
- Qualified/native runtime tests are **NOT RUN** locally. Hosted L6/qualified profiles remain responsible for those checks.
- The prior gate-contract result remains **PARTIAL (296 passed, 1 skipped)** on its recorded pre-QA candidate; it was not rerun after the final schema profile/startup changes. The exact skipped node remains `tests/gates/test_gate_contract.py:2167` because `/usr/bin/time` is unavailable locally and required by the real child runner. At that time, the final exact-HEAD hosted L6/qualified result still had to be obtained; the later verified-acceptance checkpoint records its actual outcome.

### Final-candidate open microbenchmark

The retained raw data and reproducible bounded script are [schema-open-benchmark.json](schema-open-benchmark.json) (SHA-256 `74a9d297a455ab3706ef6443a7e0ec6509d2fe09d51f10bd9b22c29e2bbfa1ad`) and [benchmark_schema_open.py](benchmark_schema_open.py) (SHA-256 `91a44a4fe750b73b5d3ea6871ae83f0f774d629af776f20db16d356e2178bd57`). On one disposable fresh schema-27 DB, 60 interleaved warm open-close pairs measured candidate median/p95 **1.523/1.850 ms** versus baseline **1.004/1.323 ms**; cold first candidate admission was **15.857 ms**. These are per-open wall-clock microbenchmark figures only.

The artifact records all 60 paired samples and actual source hashes: candidate `connection.py` `048ae5699e552c21fa9ef7c86faeb7acc38ed1ae2a5b9242b50dfa71c06c3962`, `schema_state.py` `a00fab83891bf0c3ad3d27626a7d6205af2c93d9788c9417e227f4f149ad6496`, and unchanged `schema.sql` `c48686ecde23308ca397a69a4ea3e67de7e2d86697fd6aee74c40cd9f9bf26c6`; baseline `connection.py` at `f7847a39ed89ea34a4f0a5741f1be5f4e4143354` is `42f2c1f64da3a8974ad5a2787886f7616e0523972bed1be472827d7abff9c682`. The script times `perf_counter_ns` around open+close, alternates pair order, and replays the baseline permission/PRAGMA routine without schema validation. It makes no CPU, RSS, allocation, disk-write, IOPS, or production claims.

## Review history

- Independent review found the no-replace initialization race winner skipped full integrity/FK checks. It now uses the shared read-only existing-file validator; valid and FK-invalid race winners are covered without rewriting the winner.
- Orchestrator review found bootstrap, API lifespan, and offline import could continue after failed full admission. Those paths now fail closed before cleanup, dependent writes/recovery, scheduler startup, import transaction, or file/receipt preparation. Exact known legacy schema-27 profiles were added from baseline positive migration fixtures; backup/source compatibility is retained while candidates and CLI verification stay canonical-only.
- Final grouped review rejected the temporary public `compatible_legacy` status. The public status remains the established `ok`/`drift` enum; profile identity and notes are additive metadata in schema summaries and diagnostics.
- A final diagnostics check found ordinary writer admission blocked read-only reporting of unsupported schema. Diagnostics now inspects through an existing-file read-only connection and returns the established failed database cause with dependent sections `unknown`, `executed=false`, and a not-run reason. Missing/corrupt/unsupported cases verify no create/rewrite/chmod and no dependent callbacks.

## Final local review checkpoint — 2026-11-03

Independent architecture/security/data-integrity QA **PASSED** on source manifest `0c0c00425f880fd49ba2c5d81f74c87b40a0e382502520b86a85568ebb3f6594` (42 reviewed entries). Its independent lifecycle/admin/bootstrap/maintenance/events/diagnostics run passed **185 tests**, with one existing Starlette/httpx deprecation warning. All manifest hashes/deletions and `git diff --check` were verified. Earlier race-winner integrity, public schema-status compatibility, and unsupported-schema diagnostics findings were corrected and rechecked.

The Orchestrator independently repeated the final synthetic native-proof/generation/recovery matrix: **277 passed in 21.88 seconds**. The actual fresh initializer was compared again against the approved baseline: all 47 SQLite tables (46 user tables), 129 objects, exact DDL, column/default/PK/FK/index metadata and seed semantics matched; integrity was `ok`, FK violations zero, and repeat initialization preserved database bytes. Syntax/Ruff F,E9 for all 27 changed/new Python files, the 212-file test catalog, clean-surface guard and diff check passed.

Qualified hosted/native profiles and final exact-HEAD branch L6 remain pending at this documentation checkpoint; the canonical owner-local roadmap will record their actual run and offline verification separately. Earlier local gate evidence remains explicitly PARTIAL because `/usr/bin/time` is unavailable. Source/local tests do not imply PR/main/release, deployment or production acceptance. No PR, merge, deployment, production SQLite/native/service access or mutation occurred. Packages F and G are not started; subsequent Stage 6.1 work continues on the same persistent branch.

## Hosted L6 fixture follow-up — 2026-11-03

The hosted L6 run `41540068872` on committed source `746c49f` reported seven failures in six requested test files. They exposed fixtures that still assumed historical schema-21/11 automatic upgrades, returned `None` from mocked startup bootstrap, initialized the missing-database probe through a writer session, or installed persistent main-schema triggers that the new strict schema admission correctly rejects on subsequent connections. No application contract bug was established by these failures.

The follow-up updates only tests and shared test support:

- Startup scrub and shutdown tests now provide the established healthy bootstrap result shape; startup scrub uses a freshly initialized schema-27 database and asserts its version while retaining scrub-before-scheduler and redaction assertions.
- The duplicate external `system_id` case now initializes current schema 27 twice, stores two independent connections with the same system ID, and confirms generated state survives. It no longer asserts the retired schema-11 automatic migration behavior.
- The `no_database_autoinit` route test creates an isolated SQLite fixture directly, observes it with raw read-only SQLite, and checks response, table identity, database bytes, and mode remain unchanged.
- Three transactional failure tests use connection-local TEMP triggers installed only after the ordinary current-schema admission. They still require a real SQLite `IntegrityError`, verify job/retention rollback and preserved orphan files, and retry after the temporary fault is removed. No persistent main-schema trigger is introduced.
- A route-list assertion uses the generated OpenAPI path map to remain compatible with FastAPI's router representation while preserving the same two route-presence checks.

Targeted verification from `backend/`:

`/tmp/fwrouter-private-test-venv/bin/python -m pytest -q tests/test_main_startup_scrub.py tests/test_lifespan_shutdown.py tests/test_ui_state.py tests/test_state_routes.py tests/test_job_manager.py tests/test_jobs_retention.py` — **PASS: 92 passed**, one existing Starlette/httpx deprecation warning. The initial run exposed two fixture issues (new FastAPI included-router shape and raw SQLite tuple rows); both were corrected without changing application behavior or relaxing assertions.

`git diff --check` — **PASS**. No production DB, app runtime, native runtime, workflow/gate, or catalog change was made. The exact-hosted L6 run has not yet been repeated on these follow-up sources; the previous L6 result remains failed until a fresh exact-head hosted run proves otherwise.

Updated `SOURCE_SHA256.txt` now covers 49 entries, including the seven changed test/support files. Manifest SHA-256: `750b9b3e16bf667c810e9b59da5bb63dde25c52166cbfae91f5f807c31e416f4`. This follow-up manifest is a local candidate record, not hosted acceptance evidence.

## Second hosted-L6 fixture follow-up — 2026-11-03

The retained exact-head L6 evidence for run `41540068872` showed three additional fixture categories:

- In `local-integration`, `test_offline_rebuild_rejects_unexported_durable_rows_before_backup[True]` and the custom-trigger rebuild case attempted to export from a database after adding a schema object that is now correctly refused by strict admission.
- Both hosted ordinary Compose repetitions (`hosted-125602282690.json` and `hosted-125602282932.json`) failed the old `test_provider_v23_shape_initialization_reaches_schema25_and_preserves_state`; each had 29 passing nodes and that one unsupported migration expectation failed because initialization returned schema-version 23 as not admitted. The qualified QCP/Xray and packet/provider scenarios passed in that run.

The local offline fixtures now export their snapshot from canonical schema before adding an unexported future table or custom trigger. The durable-table preflight continues to reject before backup. For triggers, strict backup validation surfaces its established sanitized `DATABASE_REBUILD_BACKUP_INVALID` boundary; the test still checks one retained backup, unchanged source logical digest, original trigger presence, and `integrity_check=ok`. A second parameter introduces a harmless additive non-unique index, which is valid for existing schema observation but must still fail the exact source-to-candidate object comparison with `DATABASE_REBUILD_SCHEMA_OBJECTS_UNSUPPORTED`. This retains both strict admission and rebuild schema-equality coverage.

The obsolete schema-23 migration test is replaced by exact current-schema idempotence coverage. It creates synthetic provider binding/credential rows, a Core intent revision, server and user routing overrides, external generated state, and subscription identity/alias/credential rows. It snapshots every SQLite table and its columns, requires exact equality after each of two initializations, checks SQLite integrity and foreign keys, then verifies loaded native Xray identities remain unchanged after the isolated worker restarts. The helper no longer permits historical Core apply-state invalidation. Schema 23 was also added to the isolated unsupported-version byte-preservation cases.

The acceptance node changed from `test_provider_v23_shape_initialization_reaches_schema25_and_preserves_state` to `test_current_schema_initialization_preserves_control_plane_and_native_state`. Its source-derived `scenarios.json` entry was regenerated using `tests/acceptance/source_catalog.py --write`; the fixed diagnostic node set and corresponding gate contract were updated to the same node. No scenario count, profile, budget, exclusion, Docker definition, or native check was changed.

Verification:

- Full `test_offline_maintenance_admission.py` and `test_database_schema_lifecycle.py` — **PASS: 65 passed**, including both custom-object branches and schema-23 rejection.
- The six-file L6 fixture compatibility suite — **PASS: 92 passed**, one existing Starlette/httpx deprecation warning.
- With pinned Ruff on PATH, `tests/gates/test_gate_contract.py tests/gates/test_acceptance_contract.py` — **PASS: 296 passed, 1 skipped**, five existing unknown `pytest.mark.browser` warnings. The skip is the existing local `/usr/bin/time` child-runner capability skip; no gate condition was changed.
- Pinned Ruff F/E9 for changed Python files, JSON parsing of `tests/acceptance/scenarios.json`, and Python compilation of the acceptance test and launcher — **PASS**. `git diff --check` — **PASS**.
- The Compose acceptance test was **NOT RUN locally** because its contract requires the qualified hosted isolated Compose/native environment. The previous hosted L6 remains failed evidence until the next exact-head run.

The updated source manifest covers 54 source entries. Its SHA-256 is `51619591cba4ab7e2ab67fd45fb16d467e46a78a92bf7e59c4cdfe1e52b5f6de`. This local report/manifest does not supersede the retained hosted failed artifacts or claim hosted acceptance.

### Qualified fixture setup review correction

Independent review found the Compose `_prepare_db` fixture seeds the current schema, module rows, and routing state but no subjects. The preservation scenario now creates its own `lan:acceptance-preserve` LAN subject (`subject_role=lan_client`, `implementation_kind=lan`) and matching `subject_lan` detail using the locally administered `02:00:00:00:00:27` MAC and documentation-range `192.0.2.27` address. It adds a test-owned active server, then binds both subject override rows to this owned LAN subject; it does not select the protected global subject or a mandatory VPN client.

The Core revision fixture no longer writes the revision value directly. With the worker stopped, it verifies the seeded Core row is enabled/clean, records a controlled disabled/pending Core intent change in the same owned SQLite transaction, and calls `services.core_intent.advance_core_intent_revision(connection)`. The returned nonzero revision and pending Core state are then covered by the full before/after table-and-row equality check. Fixture writes run with foreign keys enabled; both integrity and FK checks pass before the first repeated initialization as well as after it. Loaded native Xray identities remain a separate read-only before/after assertion; the fixture does not claim the pending intent was applied or proven by native state.

A disposable schema-27 fixture check, without Compose or native runtime, exercised the same SQL rows and Core revision owner against a temporary database and passed exact row preservation plus FK/integrity checks. The first scratch checker compared `sqlite3.Row` results directly to tuples and failed in the checker; its readback was corrected to normalize rows before comparison, then the disposable check passed. No repository assertion or application behavior was relaxed.

The final fixture-description edit changed the source-derived scenario contract, so the first focused gate-contract attempt correctly detected a stale generated `scenarios.json`. The source catalog was regenerated; its 57-node validation and the fixed four-node diagnostic contract test then passed. Final static checks and `git diff --check` passed on that regenerated catalog.

### Exact acceptance-body QA probe

Independent review found the acceptance function uses `sqlite3.Row` for its pre-initialization snapshot (needed by the Core intent owner), but tuple rows for the later raw SQLite snapshots. The shared `_database_rows` helper now converts each result row to a tuple before digesting, so its complete columns and values compare uniformly across connection configurations. The `integrity_check` result is likewise normalized when read through the row-factory connection; its real `ok` result remains required.

The retained [probe script](probe_current_schema_acceptance_fixture.py) imports and calls the actual acceptance test function and actual `application_acceptance.conftest._prepare_db` against a temporary owned state directory. It stubs only worker process lifecycle, native identity reads, and HTTP response transport; it starts no Compose stack, API service, Xray, or Mihomo runtime. Its output is **PASS** for the entire SQL/init/row-preservation test body, including FK/integrity and Core revision assertions. Its synthetic identity-read assertion is fixture wiring only and is not native proof; the qualified hosted Compose test remains required for real loaded-identity before/after verification.

Command: `/tmp/fwrouter-private-test-venv/bin/python knowledge/audits/stage6_1_package_e_2026-11-03/probe_current_schema_acceptance_fixture.py` — **PASS**. This probe reproduces the actual `_prepare_db` shape and executes the actual body; it is not a replacement acceptance node or hosted evidence.

## Resumed follow-up QA checkpoint

Final fixture-only follow-up QA **PASSED** on 54-entry source manifest `51619591cba4ab7e2ab67fd45fb16d467e46a78a92bf7e59c4cdfe1e52b5f6de`. The reviewer independently executed the retained probe, which calls the actual `_prepare_db` and full current-schema acceptance function, and verified all source digests/deletions. The probe passed all real SQLite assertions, normalizing `sqlite3.Row` values to tuples for exact snapshots and the actual integrity result. Worker lifecycle, native identity reads and HTTP transport alone were stubbed; this is not native/Compose evidence. The 57-node scenario catalog, fixed diagnostic-node contract and diff check also passed. The prior independent 157-test backend/lifecycle/offline follow-up run remains applicable to unchanged files.

No application code, workflow, gate condition, schema validation, compatibility profile, resource limit or binary pin was changed by this follow-up. The existing failed hosted run remains historical evidence. Package E remains **INCOMPLETE** until the new exact-HEAD push-fast and complete qualified branch L6 succeed, the actual Compose case passes there, and independent offline verification accepts the complete bundle.

## Verified implementation acceptance — 2026-11-03

The follow-up commit `2b79fab74c05e50342c793a2a69b0cda6f3e7479` follows implementation commit `746c49f3e16afda0af0f7044a57c8eb5a617854c` on the persistent `stage6/backend-completion` branch. It fixes the retained CI fixtures and the real acceptance body; it does not change application code, schema admission, profiles, workflow conditions, resource budgets or binary pins. The paused `sqlite3.Row` defect was fixed in both integrity-result comparison and exact row snapshots. The retained local probe calls the actual `_prepare_db` and complete acceptance function; only worker lifecycle, native reads and HTTP transport are stubbed. Independent follow-up QA passed the 54-entry source manifest and independently ran that probe, the 57-node catalog and the diagnostic-node contract.

Actual GitHub qualification on this exact commit:

| Gate | Evidence | Result |
|---|---|---|
| Push-fast | [41627311292](https://github.com/Coxf0rd/fwrouter/actions/runs/41627311292), event `push` | SUCCESS |
| Complete qualified branch L6 | [41627329040](https://github.com/Coxf0rd/fwrouter/actions/runs/41627329040), attempt 1, `workflow_dispatch` | SUCCESS, 19/19 jobs |
| Selected test inventory | Immutable full-safe plan, manifest `2026.11.03.2` | 211 selected files out of the 212-file catalog; no selected file NOT RUN |
| Results | Independently replayed `reports/visibility.json` | 3,455 unique IDs; 3,501 executions/PASS; zero FAIL, SKIP, BLOCKED or NOT RUN |
| L5 coverage | Required anchors | PASS |
| Current-schema Compose scenario | `test_current_schema_initialization_preserves_control_plane_and_native_state` | PASS in both independent hosted repetitions, distinct nonces and successful cleanup |
| Complete final artifact | `13226019998`, `fwrouter-l6-evidence-2b79fab74c05e50342c793a2a69b0cda6f3e7479-41627329040-1` | Unexpired; API and downloaded ZIP digest match |

Artifact SHA-256: `04677d109582c09304763cf0fe5962d5a40e68d5e0a07ef332c9e287ed304a9f`. The Orchestrator and independent QA separately replayed the existing `tests/gates/l6_ci.py verify` from a clean exact-source checkout, with independently established SHA, repository/workflow, event/ref, run/attempt and GitHub job/artifact expectations. Both returned `l6_evidence_verified_not_deploy_authorized`. Verification reconciled the complete content inventory, immutable plan, required result set, original producer evidence, source/build/runtime bindings, independent hosted repeats, safe-recovery evidence and observed coverage. Review evidence is retained locally under `/tmp/fwrouter-package-e-evidence-41627329040/`.

The original failed runs `41540014026` and `41540068872` remain failed historical evidence, not PASS. The old local `/usr/bin/time` skip remains accurately recorded; the complete selected hosted result has zero skipped tests. The sole catalog file omitted by the established L6 policy is optional/deferred `tests/application_acceptance/test_xray_worker_crash_l7.py`; this is explicit in the plan and is not an executed L7 test.

Scope is `branch-regression-non-promotional`, `minimum_repeats=2`, `deploy_eligibility=false`. Ordinary acceptance proves its process-backed Xray/SQLite/API/native-identity scope; it is not stock Docker/host-systemd/reboot parity or real provider traffic. Qualified native, packet, provider and browser cohorts passed under their existing separate isolation contracts. No production access, deployment, PR, merge or Packages F/G work occurred.

This documentation checkpoint records actual qualification of the implementation commit above. Its report-only follow-up commit must receive its own exact-HEAD push-fast/L6 qualification; the canonical owner-local roadmap and final handoff record those resulting run identities. Earlier pending and failed checkpoints above are preserved as history and superseded only within this verified scope.
