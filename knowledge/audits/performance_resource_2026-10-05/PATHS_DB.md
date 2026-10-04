# Stage 4 execution paths and SQLite evidence

Date: 2026-10-05
Source baseline: `0b24cd1a142b0446fc5f83fe000c7f8625c5a712` (`HEAD` at inspection).
Scope: source tracing for watchdog, selector/provider admission, subscription/runtime apply boundary, logical-member observation and SQLite; one bounded read-only SQLite snapshot. No provider requests, public probes, runtime actions, API calls, service restarts, tests, or database writes were made. This is a component report for the Stage 4 audit; measurements here are not endpoint latency or full-flow wall time.

## Method

- Read canonical roadmap, provider architecture and Test Architecture contract. Contract says default test evidence uses isolated state, production SQLite/network/runtime are excluded from routine tests, and L6 is restricted to explicit scheduled/manual, milestone, release, broad-contract or policy gates. No tests were run for this audit component.
- Traced source at the exact baseline using line-numbered reads. Relevant source references are listed below.
- Opened `/var/lib/fwrouter-v2/fwrouter.db` through SQLite URI `mode=ro`. Read PRAGMA metadata and aggregate table counts; ran `EXPLAIN QUERY PLAN` and 11 bounded repetitions each of three SELECTs matching watchdog traffic, member-probe eligibility and routing target reads. Query results were not printed. No schema inspection helper or service module was imported, avoiding initializer/scheduler side effects.
- Snapshot was point-in-time and warm-cache biased after each first sample. It does not estimate lock contention, WAL write rate, HTTP latency, runtime-controller latency, process CPU/RSS, or recurring job cost.

## Execution-path findings

### Watchdog mode admission — confirmed bounded path, no VPN recovery work on deliberate Direct with no scoped VPN subjects

`watchdog tick/API -> load watchdog module + routing -> enabled/core-bypass gates -> desired global mode + scoped-VPN-subject cache (30 s) -> pause when mode is outside VPN/selective and no scoped VPN subjects -> emergency override re-entry OR convergence/runtime/traffic path -> confirmed failure/selector/recovery -> apply/readback/state/event persistence`.

Evidence: `watchdog_auto_flow.py:40-88` runs missing/disabled/core-bypass exits before mode and scope policy; `:74-88` pauses on non-VPN/non-selective only when there are no scoped VPN subjects. This intentionally preserves scoped VPN subjects when global mode is Direct. `watchdog_status.py:89-123` computes scoped subjects from effective policy and caches the result for 30 seconds. Emergency Direct is separately admitted at `watchdog_auto_flow.py:89-99`; with `allow_switch=False` it does not call `try_verified_reentry`. Normal provider recovery is invoked only later from confirmed degraded/stalled paths (`watchdog_auto_active_quality_flow.py:364-390`, `watchdog_auto_stall_flow.py:111-136`).

Classification: **non-issue** for the policy “global Direct with fixed/scoped VPN subjects must keep their scoped VPN watchdog”; do not short-circuit all Direct ticks. **Suspected small admission overhead**: scoped-VPN effective-state calculation occurs before `emergency_override()` and before any later exit. On a cold/expired 30-second cache this can read recent Xray activity, list up to 1,000 effective subjects, and load overrides (`watchdog_status.py:89-110`, `subject_policy.py:629-672`; recent Xray activity at `ui_state_common.py:237-270`). The result is still necessary for the ordinary Direct-with-scoped-subject policy branch, so moving or skipping it needs an explicit emergency-override policy proof. Cost was not timed end-to-end. Do not label this a confirmed expensive fault.

The established path does not start selector/provider/probe/recovery work after the deliberate Direct/no-scoped-subject pause. For Direct with configured VPN-scoped subjects, later VPN-aware work is intentional. Emergency Direct retains desired VPN and enters verified re-entry; it is not equivalent to deliberate Direct.

### Watchdog ordinary tick — duplicate state construction is suspected; provider API stays gated

`watchdog scheduler/API -> module/routing/core/scope gates -> runtime convergence -> capture selection fence -> runtime controller get_state -> traffic snapshot query -> pending-recovery/idle cadence or active check -> only confirmed failure invokes selector/provider recovery -> guarded apply/readback -> module/event/state writes`.

Evidence: `watchdog_auto_flow.py:101-145, 287-318, 320-418`; the actual idle cadence check happens after runtime state and traffic signal. For Mihomo, `MihomoVpnRuntimeController.get_state()` calls `get_vpn_auto_state()` (`vpn_runtime_control.py:236-254`), and `get_vpn_auto_state()` loads candidates, queries runtime health, reads traffic accounting, and builds full diagnostic projections (`selector.py:593-631, 645-687, 731-768`). The watchdog then separately calls `detect_recent_vpn_traffic_attempts()` (`watchdog_auto_flow.py:287-289`), which queries `traffic_counter_snapshots` (`watchdog_traffic_signal.py:32-60`). The two traffic reads answer different freshness/meaning contracts, so possible shared-snapshot reuse is **suspected**, not confirmed redundant work. No live tick was invoked.

`MihomoVpnRuntimeController.get_state()` also obtains the full VPN-auto state with default `read_only=False`, which may call `ensure_routing_global_state()` (`selector.py:593-604`). This is a **confirmed write-capable helper on a watchdog read path**; whether it produces an actual write in a steady current DB is **measurement needed**. Candidate list and health are needed to determine current target validity, but all other fields in the full projection may be excess for watchdog admission. Recommended next measurement: isolated instrumented watchdog path with operation counters for DB connects/queries, controller health reads, traffic reads and projection fields, scheduler disabled and runtime/provider boundaries faked per contract. Compare cheap watchdog-state projection against full get_vpn_auto_state before proposing a source change.

### Selector/provider policy — provider API admission is bounded by chosen action

`selector call -> attribution gate for apply -> active runtime and health -> selection fence/routing state -> local candidate and runtime inventory/health -> on-demand shortlist probes only when `check_on_demand` or `apply` requests it -> choose local candidate -> only if none and provider fallback enabled load local provider candidates -> provider API operation only for selected provider candidate with apply=true and a valid runtime incarnation -> apply/readback/persistence`.

Evidence: `selector.py:1327-1380, 1411-1490, 1650-1737`; `_load_selector_candidates()` uses SQLite and canonical logical topology (`:968-1081`), while default mode is documented as stored state without pings (`:968-975`). Provider-candidate fallback begins only after no local candidate (`:1701-1711`); actual provider execution is below `apply=True` (`:1712-1737`).

Classification: **non-issue** for normal selector/provider polling: fallback may inspect provider cache/DB, but provider API is not called by that branch; Provider Adapter execution needs an applied provider switch. For a dry-run with no local selection, local provider candidate lookup can still occur. Its query count/latency was not measured and must be counted in an isolated selector profile. On-demand pings and apply/readback are explicit cost-bearing work, not hidden in the default dry-run.

### Member health observations — possible broad overlap before budget selection

`member-probe timer -> observe_effective_members for all active logical groups -> import/persist runtime snapshots -> query stale/missing member health -> rotate/select up to budget -> probe selected groups -> persist result/cursor`.

Evidence: `logical_topology.py:1116-1199` resolves every active logical group and calls native `get_logical_groups_state([...])` in one batch where supported; otherwise it takes a per-group runtime snapshot or `observe_active_member`. `probe_members()` calls this before selecting any rows under `safe_budget` and per-status TTL (`:1640-1698`). The short lane `observe_active_paths()` already selects the current routing target and reads only its effective member (`:1202-1265`).

Classification: **suspected overlap / measurement needed**, not confirmed waste. The broad read may refresh Health and can be the native adapter's intended authoritative health lane. But calling it before the sweep's own stale-TTL and budget selection means the configured probe budget bounds actual stale probes, not necessarily runtime state reads or health imports. Current DB has 428 logical servers, 3,823 members, and 1,956 member-health rows; no runtime call was measured. Measure native adapter calls, imported rows and DB writes per scheduler tick in isolated/faked boundaries and compare against the contract's active-only observation lane before changing it.

### SQLite schema health path — confirmed mutating-capable initializer on a health/read path

`GET /health` or runtime summary -> `get_cached_schema_state()` (30 s TTL) -> on cold/expired cache `initialize_database()` -> writable `db_session/connect()` -> migrations if user tables exist -> `executescript(schema.sql)` -> schema inspection -> cache result`.

Evidence: `routes/system.py:17-27`, `runtime.py:205-210`, `db/connection.py:78-108`. `initialize_database()` does not use a read-only connection; `connect()` creates/chmods DB paths, sets WAL and session pragmas (`connection.py:26-62`), and initialization can run migrations and schema DDL (`:78-88`).

Classification: **confirmed execution-path admission gap**: a read health query can reach a write-capable database initializer each cache expiration (per-process, 30 s), despite the intended operation being schema inspection. **Actual writes and cost: measurement needed**; current schema may make migration and `CREATE IF NOT EXISTS` operations no-op, and this audit did not invoke the initializer against production. Recommended fix for the later measured-fixes milestone: split pure read-only schema inspection from explicit initialization/migration; preserve migration ownership at startup. Expected effect is no migration/DDL/WAL write opportunity during steady `/health` and runtime-summary reads. Risk: reading while migration is genuinely required could report drift until the explicit migration/startup path runs; API availability semantics must be reviewed. No change made.

### SQLite connect overhead and watchdog traffic scan

`db_session()` creates/closes one connection per helper; each `connect()` ensures parent path/permissions and issues foreign-key, busy-timeout, journal-mode WAL retry, synchronous NORMAL and temp-store pragmas (`db/connection.py:26-75`). This is **confirmed repeated setup work**, while its latency/write cost in the API process is **measurement needed**. The 30 s cache avoids some repeated scope probes but not ordinary independent DB helper calls.

The watchdog traffic query is an unindexed full scan plus temporary sort on this baseline. `watchdog_traffic_signal.py:43-60` filters `path` and `collected_at`, orders recent-first and caps at 200. Read-only `EXPLAIN QUERY PLAN` reported `SCAN traffic_counter_snapshots; USE TEMP B-TREE FOR ORDER BY`; live snapshot count was 328. Eleven SQLite-only samples returned 187 rows: median 0.424 ms, max 0.836 ms across 11 samples (direct-connection timings; the first sample is cold relative to the subsequent warm-cache samples; includes Python fetch/materialization, excludes open/close and service computation). This is a **confirmed scan pattern, non-issue at observed table size**, with growth risk to remeasure before indexing.

`probe_members` candidate query uses indexed joins through server inventory, active members, topology and member-health unique key; plan ends with a temporary B-tree sort. At 73 returned candidates from current 3,823-member table, median was 0.200 ms, max 0.901 ms over 11 repetitions. Routing target read used integer primary key, median 0.0078 ms / max 0.030 ms. These are isolated SELECT timings, not function timings.

## Read-only DB snapshot

At inspection, database file `/var/lib/fwrouter-v2/fwrouter.db` was 24,563,712 bytes (5,997 pages × 4,096 bytes), `journal_mode=wal`, `freelist_count=0`; no `-wal`/`-shm` sidecars were present in the filesystem listing at that instant. Table counts included 428 servers, 428 logical topologies, 3,823 logical members, 1,956 logical member health rows, 328 traffic snapshots, 2,712 jobs, 1,872 operational logs, 405 subscription memberships, seven provider members and 23 provider evidence rows. Counts are aggregate only; no row values, payloads or credentials were emitted. This is a point-in-time production state read, not a write-growth rate. `PRAGMA user_version` was zero; `schema_meta` reports application schema version 24, so this is not evidence of schema drift.

## Evidence index

- `/srv/fwrouter/backend/fwrouter_api/services/watchdog_auto_flow.py:40-145,287-418`
- `/srv/fwrouter/backend/fwrouter_api/services/watchdog_status.py:89-123`
- `/srv/fwrouter/backend/fwrouter_api/services/subject_policy.py:629-672`
- `/srv/fwrouter/backend/fwrouter_api/services/vpn_runtime_control.py:236-254`
- `/srv/fwrouter/backend/fwrouter_api/services/selector.py:593-687,968-1081,1327-1380,1452-1490,1650-1737`
- `/srv/fwrouter/backend/fwrouter_api/services/watchdog_traffic_signal.py:32-60`
- `/srv/fwrouter/backend/fwrouter_api/services/logical_topology.py:1116-1199,1202-1265,1640-1730`
- `/srv/fwrouter/backend/fwrouter_api/services/ui_state_common.py:237-270,436-470`
- `/srv/fwrouter/backend/fwrouter_api/routes/system.py:17-27`
- `/srv/fwrouter/backend/fwrouter_api/services/runtime.py:205-230`
- `/srv/fwrouter/backend/fwrouter_api/db/connection.py:26-108`
- `/srv/fwrouter/knowledge/PROJECT_MAP/TEST_ARCHITECTURE_AND_CICD_FOUNDATION.md:24-32,46-50`
- `/решения/roadmap/fwrouter/ROADMAP.md:44-50`
- `/решения/roadmap/fwrouter/PROVIDER_MANAGED_SUBSCRIPTIONS_AND_ADAPTERS.md:29-52`

## Additional isolated path measurements

These are bounded RO service-path profiles against the same production DB using the installed FWRouter venv. `db.connection.connect` was replaced at runtime with `sqlite3.connect(file:...mode=ro)`; `sqlite3.set_trace_callback` counted only statement classes, and rows/SQL/payloads were not printed. Each path ran three times; results are warm in-process except first-call filesystem/cache effects. This is service/SQLite path cost with live state input but excludes external runtime calls; the tested functions below do not issue provider HTTP or dataplane probes.

| Read path | SQLite statements/call | Wall samples (ms), n=3 | Median | Result size |
|---|---:|---:|---:|---:|
| `get_logical_topologies()` for the 30 active logical groups | 3 SELECT | 3.67, 3.03, 2.86 | 3.03 | 30 groups / 85 active member rows represented |
| `provider_projection()` | 9 SELECT + 3 transaction BEGIN | 10.57, 5.47, 5.35 | 5.47 | 3 projected bindings (one persistent binding plus disabled/unconfigured source projections) |
| `selection_pool_signature()` | 4 SELECT | 4.21, 3.80, 3.85 | 3.85 | 64-character digest |

The topology sample resolves only active IDs: the full inventory has 428 logical groups/3,823 members, while active member joins cover 30 groups/85 member rows. The point-in-time DB has 30 active logical groups and 85 active member rows (428 total groups / 3,823 total members including inactive inventory). This narrows the expected scope of `observe_effective_members()`; it does not establish actual native call count, and native calls were not made. `provider_projection()` is safe to profile read-only: source builds from SQLite and local evidence/topology (`provider_managed.py:238-284`); no provider API call is present. It has a per-binding loop over members, topology, and latest evidence, confirmed structurally; current production count is one persisted binding, so scaling cost beyond this observed size is not measured.

### Isolated schema-cache loader cost

To quantify the `/health` cold/expired path without writing production DB, a SQLite backup of the point-in-time DB was created under a private temporary directory, then `initialize_database()` was called three times against that isolated copy with trace counters. All calls returned `schema_status=ok`. Each took 5.03, 4.15, 3.92 ms (median 4.15 ms) and emitted 22 SELECT, 77 DDL statements, 2 DML statements and 24 PRAGMA statements. The file remained 24,576,000 bytes and no WAL bytes were present after each closed invocation. This establishes that a current, already-valid DB pays about 4 ms of schema/migration/DDL/pragma work on this isolated host path, with write-capable SQL statements, while no DB size/WAL growth was observed. It does not establish production endpoint latency or whether concurrent requests are affected. The code evidence and admission-gap classification above still stand; recommend a pure read path and retain startup migration ownership.

The statement totals are from Python SQLite trace callbacks. `BEGIN` is counted under `other`; the isolated schema timing exactly measures that function plus our connection setup on a backup, not API middleware/serialization/runtime.

## Refresh and generation/reconcile path

`explicit or scheduled refresh trigger -> targeted `refresh_subscription(source_ref)` or full refresh -> provider adapter and inventory import/persistence -> compute Mihomo input fingerprint -> if unchanged return `already_current` without candidate write/validation -> apply pipeline -> Xray guard/preflight -> Mihomo reconcile rechecks runtime incarnation, active config and current input fingerprint -> unchanged branch runs its verification callback and ownership fence then writes reconcile fingerprint state -> no config promotion/container restart -> Xray/profile reconcile and terminal verified result`.

Evidence: `subscription_pipeline.py:381-434,487-617,625-650,905-932,971-1041,1108-1138`; `mihomo_reconcile.py:515-620`; fingerprint hash/state source `mihomo_reconcile_fingerprint.py:181-245`.

Classification:

- **Non-issue**: refresh input is fetched/imported before hashing; a refresh is the explicit mechanism that learns changed remote state, so an unchanged result still has required provider request cost. It is not a normal selector/Health/UI provider poll. Source policy permits explicit refresh, not unconditional runtime polling.
- **Non-issue / required safety work**: no-op reconcile still revalidates runtime incarnation, input and active config hash, invokes the verification callback and checks ownership before it skips generation promotion/restart; do not remove these fences as a performance shortcut.
- **Confirmed write-capable no-op state update; impact measurement needed**: unchanged reconcile writes a new JSON fingerprint state (`updated_at` changes) with `atomic_write_json` on every no-op reconcile (`mihomo_reconcile.py:573-603`, `mihomo_reconcile_fingerprint.py:228-245`). This is one state-file rewrite per no-op reconcile invocation after all fence checks. It is small and bounded per invocation, but contributes SSD writes if the path is frequent. No production reconcile was triggered and SSD write rate/growth was not measured. Preserve state semantics; next stage should count real invocation cadence and decide whether timestamp/event semantics require each write.
- **Suspected unnecessary projection**: `_maybe_select_vpn_auto_after_refresh()` has a non-auto fallback branch that returns a full `get_vpn_auto_state()` projection (`subscription_pipeline.py:98-127`). That projection loads all selector candidates and logical topology, calls runtime health and reads traffic accounting (`selector.py:593-687`). Fixed mode uses `read_only=True` but still constructs the full projection for target readback; other non-auto mode calls the default ensure path. This may be required for public result detail/current runtime verification, so do not call it confirmed waste. Isolated query/profile counts for these branch-specific calls are not included.

Provider recovery gates remain separate from refresh: normal selector calculation is local/cached; provider execution is explicit/confirmed recovery or selected applied provider action and remains bounded by the provider contract. No provider recovery/mutation path was invoked during this audit.

### Health projection admission

`GET /ui/settings/inventory` defaults `live_observations=true` (`routes/ui.py:163-180`) -> `_subject_health_by_subject_for_ui(blocking=True)` -> a 5-second cached `build_subject_state_projection(limit=1000)` (`ui_state_common.py:436-470`) -> read subjects/routing/overrides and derive canonical per-subject Health (`state_projection.py:778-878`) -> collect adapter Health snapshots through their own short cache (`state_projection.py:62-68`). External-source observation collection is keyed by provider type and goes through `cached_external_source_observations` (`state_projection.py:804-830`); its exact cache miss behavior depends on source connection policy. The UI inventory then separately reads its role-specific subject rows, traffic maps and subscription-client map (`ui_state_inventory.py:36-59` and later role queries). This is expected aggregation when the settings inventory is opened; cached Health and adapter snapshot reuse prevent each row rendering its own probe.

Classification: **non-issue** for the intended Health aggregation contract; **measurement needed** for cache-miss DB query count, actual adapter/external-source call count, and complete endpoint wall time. This subtask did not invoke the endpoint because a cache miss can cross runtime/external collector boundaries; no probe was issued. No claim is made that the entire Health/UI path is probe-free.
