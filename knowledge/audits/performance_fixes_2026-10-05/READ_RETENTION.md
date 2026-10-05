# Stage 4B — Read-only schema health and JSONL retention

Date: 2026-10-05  
Source baseline: `24ef1ba12c8707dc7a40dccf8bf32491a36319e7`  
Scope: backend schema health reads and log-retention rewrite admission. Code benchmarks and regression tests used isolated state. A single read-only production UI reference flow used normal GET requests; it made no production state changes, provider requests, deploys, or service restarts.

## Changes

`get_cached_schema_state()` now inspects the existing SQLite file through a `mode=ro` connection and calls the existing schema inspector. It does not call the writable `connect()` helper or `initialize_database()`. Startup and installer initialization remain on `initialize_database()`, including directory and permission setup, migrations, and schema creation. Valid but old or incomplete schemas remain visible as `drift`; SQLite open/read errors still propagate to `/health`, which returns `DATABASE_UNAVAILABLE`. The 30-second cache and Health schema projection are unchanged.

Log retention now scans the file before opening a temporary output. When no records have expired, it returns complete counters without creating or writing the temporary file. When a record expires, it rescans with the same cutoff, preserves bounded memory, line order, malformed-line retention, current blank-line handling, dry-run counters, and atomic replacement. If the expiry disappears before the rewrite pass, the temporary file is removed and the source is left untouched. The existing concurrent append/replace race during a real expired rewrite remains an open limitation; this change does not introduce a new locking protocol.

## Before and after measurements

### Schema health inspection

The reproducible paired run started from two identical copies of the same 507,904-byte temporary, initialized database. Five calls to `initialize_database()` had a 3.052 ms median; each emitted 22 SELECT, 77 DDL, 2 DML, and 24 PRAGMA statements. Five calls to the new read-only inspector had a 1.827 ms median; each opened one SQLite URI in `mode=ro` and emitted 19 SELECT, 18 metadata PRAGMA, zero DDL, and zero DML statements. The first sample on each path was cold. The paired median difference is a small local result, not a production endpoint latency claim. The SQL trace confirms that the health read no longer invokes schema/migration writes. Normal SQLite reader bookkeeping and sidecar behavior are not included in the DDL/DML count. The raw samples and counts are in [the benchmark JSON](READ_RETENTION_BENCHMARK.json); reproduce with:

```bash
PYTHONPATH=/srv/fwrouter/backend /opt/fwrouter-api/.venv/bin/python /srv/fwrouter/knowledge/audits/performance_fixes_2026-10-05/scripts/read_retention_benchmark.py
```

For context, the earlier Stage 4 DB audit used a 24,576,000-byte snapshot, n=3: `initialize_database()` median 4.15 ms, 22 SELECT, 77 DDL, 2 DML, and 24 PRAGMA statements. That historical measurement is retained in [the Stage 4 DB evidence](../performance_resource_2026-10-05/PATHS_DB.md); its latency is not directly compared with the paired smaller-database run above.

### JSONL retention with no expired lines

The before and after runs used the same synthetic 2,020,000-byte file with 1,000 valid 2,020-byte JSONL lines, five repetitions, and `dry_run=False`:

| Measure | Before | After |
|---|---:|---:|
| Median wall time | 8.55 ms | 5.57 ms |
| Temporary bytes written per call | 2,020,000 | 0 |
| Expired lines / rewrites | 0 / 0 | 0 / 0 |

Before samples: 9.01, 8.55, 8.03, 8.48, 10.22 ms. After samples: 5.63, 5.57, 5.51, 5.51, 5.79 ms. The elapsed-time reduction is small and host-specific; the measured result is removal of 2.02 MB of unnecessary temporary writes for this no-expiry case. This does not establish a material endpoint-latency improvement. These measurements are generated alongside the DB comparison by the reproducible command above.

## Regression evidence

- L0 static checks: `py_compile` on changed Python files and `git diff --check` passed.
- Focused new regression tests: 8 passed. They cover read-only schema inspection, no startup initializer call, preserved schema drift, missing-database `DATABASE_UNAVAILABLE` without file creation, no-expiry append preservation/no temp open, expired rewrite order/malformed lines/counters, dry-run behavior, source replacement between the expiry pre-scan and rewrite pass, and temporary-file cleanup/handle closure on a write error.
- Affected backend cohorts (`test_db_migrations`, `test_runtime_logs`, `test_live_probe_cache`, `test_runtime_summary`, `test_health_contract`, `test_bootstrap`, `test_main_startup_scrub`): 58 passed, 2 failed. Both failures are pre-existing observations listed in the [test baseline audit](../test_architecture_cicd_2026-10-04/BASELINE_AUDIT.md): Mihomo reconcile result assertion and startup selector result-shape assertion. Rerun with a separate pytest `--basetemp` reproduced them.
- Database L5 regression anchors: Python and installer files yielded 64 passed, 6 failed; UI domain-model and action anchors passed 2/2 with Node. All six failing nodes, plus the two failures in the affected backend cohort, were rerun on a clean detached worktree pinned to `24ef1ba`; all eight failed there with the same assertions. The Mihomo/startup and State API failures are listed in the [test baseline audit](../test_architecture_cicd_2026-10-04/BASELINE_AUDIT.md). Four Home Assistant action assertions expect message field names different from the current implementation; baseline reproduction confirms these are stale test expectations, not regressions from this patch.
- The deterministic affected-path plan selected L0–L3 plus database L5 anchors, but `gate.py run` rejected its execution because the source edits are uncommitted and its path matcher requires an immutable `base..HEAD` diff. The direct isolated cohorts above were run instead. L6 was not run.
- All eight cohort failures were rerun on a clean detached worktree at baseline `24ef1ba12c8707dc7a40dccf8bf32491a36319e7`, using `/opt/fwrouter-api/.venv/bin/pytest`, `PYTHONPATH` pointed at that worktree's backend, and a separate `--basetemp`; all eight reproduced. Exact nodes:

  ```text
  backend/tests/test_runtime_logs.py::test_mihomo_reconcile_skip_writes_only_technical_log
  backend/tests/test_bootstrap.py::test_recover_startup_mihomo_selector_restores_active_auto_target
  backend/tests/test_state_routes.py::test_state_endpoints_are_get_only_and_read_only
  backend/tests/test_state_routes.py::test_read_endpoints_do_not_bootstrap_builtin_subjects_or_routing_state
  installer/tests/test_homeassistant_action.py::test_switch_best_confirms_changed_logical_server_and_reports_provenance
  installer/tests/test_homeassistant_action.py::test_switch_best_keeps_noop_distinct_from_change
  installer/tests/test_homeassistant_action.py::test_switch_best_verifies_fixed_effective_server_after_auto_selector_change
  installer/tests/test_homeassistant_action.py::test_switch_best_direct_effective_route_does_not_fall_back_to_logical_id
  ```

  Reproduction command (run from the source checkout):

  ```bash
  git worktree add --detach /tmp/fwrouter-stage4b-clean-baseline-20261005 24ef1ba12c8707dc7a40dccf8bf32491a36319e7
  PYTHONPATH=/tmp/fwrouter-stage4b-clean-baseline-20261005/backend /opt/fwrouter-api/.venv/bin/pytest -q --basetemp=/tmp/fwrouter-stage4b-clean-baseline-tests-20261005 \
  /tmp/fwrouter-stage4b-clean-baseline-20261005/backend/tests/test_runtime_logs.py::test_mihomo_reconcile_skip_writes_only_technical_log \
  /tmp/fwrouter-stage4b-clean-baseline-20261005/backend/tests/test_bootstrap.py::test_recover_startup_mihomo_selector_restores_active_auto_target \
  /tmp/fwrouter-stage4b-clean-baseline-20261005/backend/tests/test_state_routes.py::test_state_endpoints_are_get_only_and_read_only \
  /tmp/fwrouter-stage4b-clean-baseline-20261005/backend/tests/test_state_routes.py::test_read_endpoints_do_not_bootstrap_builtin_subjects_or_routing_state \
  /tmp/fwrouter-stage4b-clean-baseline-20261005/installer/tests/test_homeassistant_action.py::test_switch_best_confirms_changed_logical_server_and_reports_provenance \
  /tmp/fwrouter-stage4b-clean-baseline-20261005/installer/tests/test_homeassistant_action.py::test_switch_best_keeps_noop_distinct_from_change \
  /tmp/fwrouter-stage4b-clean-baseline-20261005/installer/tests/test_homeassistant_action.py::test_switch_best_verifies_fixed_effective_server_after_auto_selector_change \
  /tmp/fwrouter-stage4b-clean-baseline-20261005/installer/tests/test_homeassistant_action.py::test_switch_best_direct_effective_route_does_not_fall_back_to_logical_id
  ```

- The isolated L4 smoke gate passed with native validation not requested. Command: `cd /srv/fwrouter && /opt/fwrouter-api/.venv/bin/python tests/gates/gate.py smoke --profile isolated > knowledge/audits/performance_fixes_2026-10-05/L4_ISOLATED_SMOKE.json`. The JSON evidence records the isolated target and database paths.
- UI reference: [single pre-deploy User → Admin → Settings run](UI_PREDEPLOY_REFERENCE.json), captured by [the reusable existing-Chromium harness](scripts/ui_predeploy_reference.mjs). Command: `FWROUTER_UI_PERF_OUT=/srv/fwrouter/knowledge/audits/performance_fixes_2026-10-05/UI_PREDEPLOY_REFERENCE.json node /srv/fwrouter/knowledge/audits/performance_fixes_2026-10-05/scripts/ui_predeploy_reference.mjs`. It observed usable User content at 3.228 s (3.500 s total), Admin content at 5.011 s (5.270 s total; 6 role, 31 server, and 4 device rows), and Settings content at 0.422 s (0.684 s total; 64 inventory rows, no journal rows needed). It recorded 18 GET API requests with HTTP 200, zero non-read attempts, and zero visible DOM alert/status errors. The `/api/v2/ui/external-ip` probe was blocked before dispatch; one console error was recorded with that intentional abort, and no page-level JavaScript exception was observed. Browser HTTP cache was disabled for this run; there is no warm-browser-cache comparison, and backend cache state was not changed. Existing local view/locale values were restored. This is one point-in-time flow, not a strict paint benchmark.

## Deploy preparation (not executed)

The approved next deployment scope is `/srv/fwrouter/installer/install.sh --deploy --component backend --component docs`, then an explicit restart of `fwrouter-api.service` only. The API-to-gateway lifecycle is observed during verification; no new restart plan is introduced for the gateway. Do not deploy or restart until the reviewed immutable source commit and root's explicit GO are recorded.

Immediately before deploy, capture a protected `0700` backup directory outside Git with files mode `0600`: SQLite online backup (never an offline copy of a live WAL database), used `/opt/fwrouter-api/.env`, current source archive for the exact commit, deployed backend/docs source archive, and relevant generated/runtime configuration snapshots. Record redacted provenance/revision, current selection/member/exclusive state, parity/Health readback, API and provider-process metrics, and container IDs plus Docker `StartedAt` for Mihomo and Xray. Compare native config hashes and Xray binding semantic digest before and after. Check for active jobs and in-progress mutations immediately before restart; if present, wait without cancelling them. Verify bounded normal GET endpoints and rerun the same UI harness to a distinct `UI_POSTDEPLOY_REFERENCE.json`. Do not call provider APIs, mutate desired state, regenerate/refresh config, or restart Mihomo/Xray. Abort on changed intent, binding/parity/Health invariant, unexpected runtime incarnation, or failed readback; rollback code only through the installer and never restore an older database. Since this patch only changes read helpers, unchanged native config hashes plus healthy pre/post readback are the expected native proof.

## Delivery boundary

Source and targeted tests are present in the worktree. Commit, deployment, service restart, production DB/log access, and live verification were not performed.
