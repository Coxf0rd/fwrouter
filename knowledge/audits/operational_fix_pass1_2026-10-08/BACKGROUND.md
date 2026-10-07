# Operational Performance Fix Pass 1 — background audit

Date: 2026-10-08. Source baseline: `4bb23916fbe346d59e42f91eb6883903e7b59316`.

This background workstream changed source in two approved details: removed duplicate `JobManager` cleanup calls where the called service path already performs the same cleanup, and made identical canonical Settings saves skip the write, cache invalidation and prewarm. It did not change bootstrap normalization or maintenance behavior. No production DB, process, service or provider state was touched. The historical audit candidates below were reviewed for safety and either resolved narrowly or retained.

## Existing evidence

The prior [Operational Latency & Resource Audit](../operational_latency_resource_2026-10-07/REPORT.md) identifies duplicate stale-job cleanup admissions, warm bootstrap normalization/DDL and Settings-save prewarm as cost candidates, but leaves attribution and safety semantics open.

The isolated warm-bootstrap cohort in `operational_latency_resource_2026-10-07/isolated_startup_foundation_metrics.json` measured four warm runs at 9.108–9.522 ms, 7.253–7.850 ms process CPU, 320–368 process-accounted write bytes, and 131 SQL statements per call: 26 SELECT, 19 PRAGMA, 77 CREATE, 4 INSERT, 2 UPDATE, 1 DELETE and 1 COMMIT. It found no stale jobs or legacy taxonomy rows in those runs. The first fresh-schema call was 31.495 ms, 19.192 ms CPU, 131 SQL statements, and created the 507,904-byte database. Lifetime process peak RSS (43,792 KiB) includes imports/setup and is not a per-call peak. WAL was zero at the captured post-call points; no physical SSD or fsync conclusion follows.

The Settings-save control cohort in `CONTROL_OPERATIONS.md` measured five requests at p50 10.324 ms / max 15.311 ms, five SELECT + five INSERT + five COMMIT, and five prewarm dispatches. The benchmark stubbed the prewarm worker, so it gives no prewarm completion CPU/RSS/I/O cost. Its sampled cohort CPU/RSS includes observer overhead and is not action-attributed for this short operation.

## Historical baseline findings (pre-fix)

### JobManager and job service cleanup

`jobs/manager.py` calls `cleanup_stale_jobs()` in `JobManager.create()`, then `services/jobs.py:create_job()` immediately calls `cleanup_stale_running_jobs()` again. A locked create then calls `get_active_lock_lease()` → `find_active_lock_conflict()`, which performs a third stale cleanup before querying active queued/running rows. These sweeps are sequential in one call and use the same timeout predicate. The later conflict query and SQLite unique constraint still own race detection; the redundant sweeps do not strengthen that fence.

Read paths also repeated the pattern: manager `get_job()` / `list_jobs()` cleaned before service functions, which clean again. `start_job()` cleaned then called service `get_job()`; `wait_for_job()` cleaned each poll then called `get_job()`. The approved change removes those manager-layer duplicates while preserving service-level standalone cleanup. Explicit `run_job()` worker cleanup and the service transition cleanup remain.

The prior job-create benchmark reports 70 SELECT, 15 DML and 15 commits across five complete create/run/terminal no-op requests. It does not isolate stale cleanup SQL, so it cannot quantify cleanup's contribution. No before/after measurement for this path has yet been taken in this workstream.

### Bootstrap normalization

`bootstrap_backend()` always runs `initialize_database()`, `cleanup_stale_running_jobs(stale_after_seconds=0)`, `normalize_subject_taxonomy()` and `ensure_builtin_system_subjects()` when the schema is valid. Taxonomy normalization first selects only legacy `tailscale` / `tailscale_node` rows and only runs its update/logging when such rows exist; removing the scan would risk missing supported legacy state. `ensure_builtin_system_subjects()` checks each canonical subject, then unconditionally updates its core row and upserts its detail row; the warm evidence shows this persists two UPDATEs and four INSERT-class statements despite `normalized_count=0`. Those updates enforce canonical system-subject fields and refresh `updated_at`, so they cannot be dropped as no-ops without a complete conditional comparison preserving the same repair semantics. `initialize_database()` executes the schema script and inspects schema on every startup; its DDL is idempotent, while migration and schema validation are startup correctness gates. The evidence does not support removing those gates.

Stale cleanup at startup uses a zero-second cutoff, intentionally failing queued/running jobs as startup orphans. Preserve this recovery behavior. Any future optimization should make the zero-stale-row path avoid write transaction overhead only if tests prove the startup cutoff and concurrent admission semantics unchanged.

### Settings save and prewarm

`services/ui_state_settings.py:save_ui_display_settings()` reads external connection inventory, normalizes the payload, writes the settings row, clears the live-probe cache and dispatches asynchronous prewarm after every call. The prewarm includes runtime summary, system summary, client list, router summary and Settings workspace. Settings are consumed by UI projections and also by `runtime_adapters`, `external_collectors` and `external_vpn`; changed-value behavior must retain existing cache invalidation and prewarm until narrower dependencies are proved.

The approved no-op gate compares canonical serialized stored data with the normalized stored payload. Absent, malformed, legacy-shaped, type-different or changed rows retain the durable UPSERT, cache invalidation and prewarm behavior. The comparison excludes transient external registry projection data.

## Pass 1 source changes and matched evidence

Root approved two narrowly scoped changes. `JobManager` no longer performs an outer stale cleanup immediately before calling service APIs that already clean with the configured timeout (`create`, `get`, `list`, `start`, and `wait`). The service admission checks, public standalone service cleanup, explicit worker cleanup, stale queued/running predicates, timeout configuration and conflict fences remain. The stale queue lock test confirms a queued job older than the timeout is failed and its lock becomes available to a new create.

An identical canonical display-settings save now reads and compares the persisted stored payload in the same database connection. Exact canonical JSON equality returns without DML, `COMMIT`, cache clear or prewarm dispatch. Missing, malformed, legacy-shaped, type-different or changed data still uses the existing durable UPSERT and triggers cache invalidation and prewarm. Scalar JSON types are compared through canonical serialization, so `true` is not treated as numeric `1`.

Reproducers: `job_manager_cleanup_bench.py` and `settings_noop_bench.py`; captured counts are in [BACKGROUND_METRICS.json](BACKGROUND_METRICS.json).

For 100 unlocked `JobManager.create()` calls on a private temp DB, the old/new cohort measured 568.028/471.417 ms wall and 376.261/276.869 ms process CPU. Cleanup calls fell 200→100, connections 300→200, and SELECTs 300→200; job INSERT/BEGIN/COMMIT remained 100 each. Current RSS moved 37,632→37,640 KiB and 37,644→37,652 KiB respectively. Process-accounted `write_bytes` fell 14,008,320→10,731,520, while `wchar` was effectively unchanged; this is not physical SSD/fsync evidence. WAL stayed zero in post-action samples and final DB size was 532,480 bytes in both fixtures. One cohort per mode is directional; no production latency claim follows.

The Settings benchmark was repeated after the type-aware canonical serialization change. For 100 identical canonical saves, wall/CPU were 206.856/206.769 ms before and 202.795/202.647 ms after; this small single-cohort difference does not establish a latency win. Connections stayed 200; DB work changed from 100 SELECT + 100 INSERT + 100 BEGIN + 100 COMMIT to 200 SELECT and no DML/transaction commit. Cache invalidation and prewarm dispatch dropped 100→0. Current RSS stayed near 44.4 MiB. Process-accounted `write_bytes` was equal at 6,553,600, so no I/O reduction claim is made. The prewarm worker was stubbed and only dispatches were counted. Native and provider calls were zero in both isolated cohorts.

Both timing scripts use the existing Python monotonic and process clocks outside any sampler. `ru_maxrss` is a process-lifetime high-water mark, not an operation peak; one after-process value was anomalous at 498,608 KiB despite current RSS staying around 37–44 MiB, so it is not used as a peak resource result. No `sleep()`, retries, persistent cache, index, concurrency or parallelism change was made.

## Candidate decision and measurement boundary

Warm bootstrap remains safety-sensitive and the evidence does not justify skipping schema initialization, migrations, taxonomy normalization or builtin subject repair. Settings changed-save prewarm remains in place because the setting participates in external collector/runtime adapter behavior and prewarm completion was not measured. The measured source diff contains only the approved job and identical-save no-op details.

## Required tests if approved

The direct affected service suites `test_job_manager.py` and `test_ui_state.py` pass (**69 tests**). They cover one cleanup on an unlocked manager create, stale queued lock recovery, stale running cleanup, exact identical-row preservation/no DML/COMMIT/cache/prewarm, type-aware canonicalization, and changed-save invalidation/prewarm. No L6/L7 or native Mihomo/Xray validation was run. The gate's immutable `base..HEAD` planner rejects the uncommitted working diff, so no affected-change gate receipt is claimed here; root integration should run it after the coherent commit.
