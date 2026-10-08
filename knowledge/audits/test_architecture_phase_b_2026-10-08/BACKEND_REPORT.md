# Backend test architecture Phase B report

Date: 2026-10-08
Scope: `backend/tests`, `backend/pyproject.toml`, and test-only support files.
Production code, deployment targets, and live services were not modified.

## Isolation and bootstrap

`backend/tests/conftest.py` now loads the standard-library-only `_isolation_bootstrap.py` before importing any `fwrouter_api` module. Bootstrap fails if an application module is already in `sys.modules`, disables bytecode writes before helper loading, clears inherited environment values, and sets an explicit test-only environment with HOME, XDG, TMPDIR, state, and run paths inside an owned temporary root. It disables startup schedulers and recovery, prevents dotenv loading before other app imports, and checks that Settings paths and the Xray writer lock resolve inside owned state.

The process guard denies Python subprocess/exec/fork events, all socket events except AF_UNIX socket creation for `socketpair`, all socket connects, protected production and credential path reads, SQLite connections outside owned state, and file writes outside the owned root. It covers tested `open`, `listdir`, `scandir`, `sqlite3.connect`, and filesystem mutation audit events, including dirfd mutation resolution. The pytest cache provider is disabled, bytecode writes are disabled, and assertion rewriting is plain so test outputs remain under the validated root. Direct pytest creates its own `/tmp` root; the gate runner may supply a private current-user coordinator root under `/tmp` with a regular ownership marker and an in-root node receipt. Only the coordinator removes shared suite roots, after reading receipts. Tests that retain artifacts must opt in explicitly.

This is a process-level Python guard, not an OS sandbox. It does not claim to block native extensions or direct syscalls, and `os.stat` is not treated as a guaranteed audit event. Python SQLite connection auditing does not establish a boundary against SQL `ATTACH` or native SQLite extensions. No untrusted tests, native tests, recovery tests, or live-runtime tests were run. Guard checks prevent known unsafe Python paths before their effects; they do not provide kernel confinement.

The shared default Mihomo singleton is retargeted from its import-time production config and contours paths to the current test state. Its default health/read/apply methods report `not_configured`, empty inventory, no active target, or failure. The default Xray adapter is no-op. The global dataplane fake, live-mode probe, and Mihomo restart seam now fail closed; they no longer claim apply, enforcement, or restart success. Tests that need successful simulations must provide explicit test-local fakes. The existing DB persistence probe was renamed to `test_enable_vpn_module_persists_desired_state`; it queries the `vpn` row and asserts the enabled desired state instead of printing and passing unconditionally.

## Shared setup helpers

An AST comparison against the pre-change test files found 29 exact `_configure_env` copies in three behavior groups: 18 tests that set `FWROUTER_STATE_DIR` and clear cached Settings; 9 that also clear the live-probe cache; and 2 that disable the maintenance, watchdog, and runtime-convergence schedulers. These now import the matching helper from `backend/tests/_test_support.py`. The common helper resolves `get_settings` locally and preserves the original order of environment setup, cache clearing, and optional live-probe cache clearing. The scheduler-specific flags remain in their dedicated helper. Module-specific helpers that reset additional runtime/watchdog state, set extra environment values, or initialize schemas were left local.

The identical `_database_snapshot` implementation was moved to `_test_support.py`. Read-only endpoint assertions now compare complete ordered application-table snapshots, and failure messages show exact added and removed rows. These changes strengthen the old count-only assertions; they do not weaken the read-only contract. There is no comparable pre/post performance benchmark, and this work makes no speedup claim.

The helper standardization touched 29 existing test modules. Across all backend test sources, 36 `test_*.py` modules were modified or added; three additional support files were touched (`conftest.py`, `_isolation_bootstrap.py`, `_test_support.py`). AST accounting found 12 existing test definitions changed or migrated, plus 4 new test definitions. The existing-definition count includes the rename from `test_debug_db_state`; the four new definitions are the positive persistence contract and three isolation tests. These are specific changed definitions, not a claim that the full backend catalog was refactored.

## Environment and bounded checks

The deployed `/opt/fwrouter-api/.venv` was not used or changed. A temporary venv at `/tmp/fwrouter-phaseb-20261008/venv` was created from the hash-pinned `tests/gates/requirements-ci.txt` using `pip --require-hashes`. It contains Python 3.11.2, pytest 8.3.5, FastAPI 0.115.12, Pydantic 2.10.6, pydantic-settings 2.7.1, and httpx 0.28.1. Pytest plugin autoload was disabled in each test process.

The fresh-child guard probe passed 18/18 checks:

```text
/usr/bin/python3 tests/gates/isolation_smoke.py
```

Checks covered clean environment construction; owned state; deployed and arbitrary dotenv denial; credential reads; production and encoded SQLite paths; symlink aliases; subprocess and exec; IPv4/IPv6 sockets and UNIX connect; dirfd rename; allowed UNIX socketpair and owned SQLite; and owned artifact cleanup. The command completed successfully in approximately 0.03 seconds. Its claim is limited to the audited Python events listed by the probe.

The three isolation tests plus the persistence and deterministic debounce representatives passed:

```text
env -i PATH=/usr/local/bin:/usr/bin:/bin PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 PYTHONDONTWRITEBYTECODE=1 \
  /tmp/fwrouter-phaseb-20261008/venv/bin/python -I -m pytest -q --tb=short \
  tests/test_isolation_bootstrap.py \
  tests/test_apply_pipeline.py::test_enable_vpn_module_persists_desired_state \
  tests/test_xray_debounce.py::test_worker_serializes_overlap_and_does_not_clear_skipped_module
```

Result after the fail-closed default change: **5 passed in 0.61 seconds**. The debounce test now waits on Events and observes the second caller reaching the real writer guard while the first call is held; it then confirms the existing serialization result. No sleeps or fake lock behavior were added.

Shared-helper representatives also passed:

```text
tests/test_dataplane_global.py::test_runtime_check_paths_uses_manifest_without_revalidating_applied_file
tests/test_custom_servers.py::test_servers_list_excludes_virtual_xray_vpn_auto_by_default
```

Each passed independently with the isolated venv. A separate exploratory representative, `test_runtime_summary_contains_layout_and_modules`, attempted router DNS inventory through `subprocess.run`; the guard denied it before process launch. It is excluded from the passing set and needs a reviewed test-local inventory fake if selected in a later suite.

Static validation parsed all 125 Python files under `backend/tests` with the system Python AST parser, and `git diff --check` passed for backend tests, `pyproject.toml`, and this report.

The gate's isolated FastAPI smoke helper was invoked directly with the pinned Python child, without running installer or native validation. Result: `health_api=passed` with `guarded_fastapi_testclient`; `critical_state_api=passed` with `guarded_fastapi_testclient_fixture_projection`. Outer wall time was 1.562 seconds and maximum child RSS was 82,844 KiB. The second result is a mocked state projection and remains partial L4 evidence; neither result establishes full-app L4 acceptance.

A gate-compatible exact-node coordinator probe used `-p gate_plugin`, disabled plugin autoload and bytecode writes, used the gate's marked private suite root, a basetemp below that root, and a node receipt in the same root:

```text
/tmp/fwrouter-phaseb-20261008/venv/bin/python -m pytest -p gate_plugin -p no:cacheprovider \
  --basetemp <private-suite-root>/pytest-tmp \
  tests/test_bootstrap.py::test_recover_startup_mihomo_selector_restores_active_auto_target
```

Result: **1 passed in 0.46 seconds**; outer elapsed time 0.924 seconds; maximum RSS 65,688 KiB. The parent parsed the exact-node receipt before deleting the outer owned root; cleanup was confirmed. This was one exact-node coordinator probe, not a full gate plan or suite.

## Historical unresolved cohort

No unguarded historical harness was executed. Before the test-local fixture corrections, the guarded 11-node cohort produced **0 passed / 11 failed** in 3.755 seconds outer time (pytest 3.24 seconds, maximum RSS 130,104 KiB). Its initial failures showed the expected production-path singleton capture, subprocess attempts, fixture/contract mismatches, and the incomplete lifecycle stub. After bootstrap and reviewed test-fixture corrections, the exact cohort was rerun using the pinned venv:

```text
env -i PATH=/usr/local/bin:/usr/bin:/bin PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 PYTHONDONTWRITEBYTECODE=1 \
  /tmp/fwrouter-phaseb-20261008/venv/bin/python -I -m pytest -q --tb=line \
  tests/test_bootstrap.py::test_recover_startup_mihomo_selector_restores_active_auto_target \
  tests/test_mihomo_config_runtime.py::test_reconcile_mihomo_runtime_skips_restart_for_unchanged_config \
  tests/test_mihomo_config_runtime.py::test_reconcile_mihomo_runtime_skips_candidate_generation_when_inputs_unchanged \
  tests/test_performance_optimizations.py::test_runtime_topology_batch_matches_single_and_uses_one_observation \
  tests/test_reconcile_core.py::test_reconcile_endpoint_returns_contract_and_is_read_only \
  tests/test_runtime_logs.py::test_mihomo_reconcile_skip_writes_only_technical_log \
  tests/test_state_routes.py::test_state_endpoints_are_get_only_and_read_only \
  tests/test_state_routes.py::test_read_endpoints_do_not_bootstrap_builtin_subjects_or_routing_state \
  tests/test_xray.py::test_vpn_auto_invalid_stage_keeps_database_and_active_xray_unchanged \
  tests/test_xray.py::test_legacy_subscription_get_is_read_only \
  tests/test_xray_vpn_auto_lifecycle.py::test_vpn_auto_reconcile_removes_stale_identity_and_recreates_one_stable_identity
```

Result after the global routine defaults were changed to fail closed: **4 passed / 7 failed** in 2.38 seconds pytest time (2.913 seconds outer; maximum RSS 99,748 KiB). The complete per-node disposition is:

| Historical node | Result | Exact disposition |
| --- | --- | --- |
| `test_bootstrap.py::test_recover_startup_mihomo_selector_restores_active_auto_target` | PASS | Updated the test fixture to the current selector runtime seam, seeded the required eligible preference, and used stateful in-memory selector operations with stable synthetic incarnation. Core applies stale `vpn-auto` to `srv-norway`, verifies readback, then restores `vpn-global`; the test asserts both calls and the persisted Core selection fence. |
| `test_mihomo_config_runtime.py::test_reconcile_mihomo_runtime_skips_restart_for_unchanged_config` | PASS | Test-local synthetic runtime incarnation exercises the unchanged-config branch; restart assertion remains. |
| `test_mihomo_config_runtime.py::test_reconcile_mihomo_runtime_skips_candidate_generation_when_inputs_unchanged` | PASS | Test-local synthetic runtime incarnation exercises the fingerprint skip; generation, validation, and restart tripwires remain. |
| `test_performance_optimizations.py::test_runtime_topology_batch_matches_single_and_uses_one_observation` | FAIL | Assertion expects no single calls, actual fake reports `['Single', 'Unavailable']`; batch call is `['Multi', 'Unavailable']`. The helper behavior/test oracle needs a product/design decision. No calls assertion was removed. |
| `test_reconcile_core.py::test_reconcile_endpoint_returns_contract_and_is_read_only` | FAIL | Full table diff shows one added row in `watchdog_state`: id 1, all state fields null, timestamp set. |
| `test_runtime_logs.py::test_mihomo_reconcile_skip_writes_only_technical_log` | PASS | Test-local synthetic runtime incarnation; both operational-log absence and technical-log event assertions remain. |
| `test_state_routes.py::test_state_endpoints_are_get_only_and_read_only` | FAIL | Full table diff shows the same lazy `watchdog_state` id 1 row added while issuing GETs. |
| `test_state_routes.py::test_read_endpoints_do_not_bootstrap_builtin_subjects_or_routing_state` | FAIL | Full database snapshot sees the same watchdog row. Its specific subjects and routing snapshots remain unchanged; the broader read-only snapshot still fails. |
| `test_xray.py::test_vpn_auto_invalid_stage_keeps_database_and_active_xray_unchanged` | FAIL | Staging rejection and Xray config assertions pass, but full database diff shows the same watchdog row added. The local DNS discovery fake prevents external process launch. |
| `test_xray.py::test_legacy_subscription_get_is_read_only` | FAIL | HTTP 200 and `not_configured` response assertions pass; full database diff shows the same watchdog row added. |
| `test_xray_vpn_auto_lifecycle.py::test_vpn_auto_reconcile_removes_stale_identity_and_recreates_one_stable_identity` | FAIL | Existing `_install_reconcile_stubs` replaces the tested `reconcile_xray_subscription_profile_nodes` function with `{ok,status,nodes_count}` and omits `deleted_count`; the test gets `KeyError: deleted_count`. The stale-identity assertions and product counters were retained. This test remains a Phase C migration followup. |

The five DB failures share one observed row delta, not five separate data changes. `watchdog_runtime_state.ensure_watchdog_runtime_state_row()` is the source writer for the lazy id-1 seed. Follow-up should identify and remove the read-path dependency on this insert or make absent watchdog state a read-only empty projection; no application code was changed in Phase B. The topology mismatch and lifecycle stub remain distinct test/product followups.

## Remaining followups and limits

- Product followup: preserve the read-only behavior when a watchdog state row is absent. The current observed diff is a lazy `watchdog_state` seed.
- Test/product decision: reconcile the batch-topology test's “zero single calls” oracle with the observed `Single` and `Unavailable` fallback calls while preserving the batch comparison.
- Phase C test migration: exercise lifecycle stale-identity behavior through the current generation/publication path; do not return fabricated counters from the stub.
- The exploratory runtime-summary node still needs a per-test fake for router DNS discovery before it can be considered under the process guard.
- The 11-node cohort is **not green**, the full backend suite was not run, and no Phase C, native, recovery, deploy, or live-runtime acceptance is claimed.

## Final static and SQLite cleanup review

An AST comparison against parent commit `38345d4` matched all 29 migrated `_configure_env` bodies, grouped as 18 basic state/cache setups, 9 setups that also clear the live-probe cache, and 2 setups that disable the three schedulers. The body comparison ignores only function signature annotations; the executable statement bodies match the corresponding shared helper contracts and preserve call order. A load-reference comparison found 27 newly unused `get_settings` and `clear_live_probe_cache` imports across 23 migrated modules; those imports were removed. The same check now finds zero such newly unused imports among the 29 migrated modules.

The owned-SQLite isolation test now uses a function-scoped `tmp_path`, explicitly commits, and closes its connection with `contextlib.closing`; this avoids both connection leakage and collisions when the node is invoked again. With the pinned venv and isolated environment, `tests/test_isolation_bootstrap.py` passed **3 tests in 0.49 seconds**. The SQLite-only node also passed in two separate invocations (**1 passed in 0.42 seconds**, then **1 passed in 0.44 seconds**). No historical 11-node rerun was needed because this change only affects the isolation probe.
