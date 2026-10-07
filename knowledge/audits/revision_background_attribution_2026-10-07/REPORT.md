# VPN-auto revision background attribution — 2026-10-07

## Scope and result

Read-only attribution of the persistent `routing.auto_selection_revision` change from 37 in the protected predeploy snapshot to 65 in the October 7 acceptance capture. No production database, runtime, route, service, or provider state was changed. No provider request, probe, refresh, test suite, deploy, or restart was initiated for this audit.

**Classification: 22 increments have numeric job receipts; 6 increments are expected/inferred from source and scheduled-refresh event evidence, with no retained numeric receipt.** Together these account for all 28 increments as runtime-generation fencing. No selected logical-server switch, redundant increment, or unexplained increment is evidenced in this interval. The early six are not promoted to observed numeric history.

Source HEAD was `3fc496a`; deployed application was `03a64a1`. SHA-256 matched source and deployed copies for `xray_subscription_service.py`, `mihomo_reconcile.py`, `mihomo_runtime.py`, `vpn_auto_selection_state.py`, and `logical_topology.py`.

## Revision writer inventory

The two SQL write primitives are `advance_selection_revision()` and `commit_active_selection()` in `vpn_auto_selection_state.py`. The latter commits active server/provenance plus one revision in a CAS transaction. The former performs one settings upsert and starts an immediate transaction when the caller has not already opened one. No database trigger writes this setting.

All source call sites in the reviewed baseline are:

| Area | Functions using a revision primitive | Intended trigger |
|---|---|---|
| Selection | `selector._reconcile_observed_selection_after_cas_miss`, `restore_auto_selection_snapshot`, `restore_core_vpn_auto_runtime_target`, and both `select_vpn_auto_server` commit branches | CAS selection/provenance commit or restoration; the runtime-target restore path fences the write/apply |
| Eligibility and inventory | `server_preferences.update_server_preferences`, `replace_vpn_auto_servers`; `server_inventory.sync_servers_from_mihomo`; `custom_servers._selection_pool_fence_finish`; `subscription.delete_subscription_source_intent`, `_upsert_subscription_servers`; `server_state._clear_expired_global_fixed_server_state` | Advance when the eligible pool, configured membership, source inventory, or expiring fixed-mode intent changes; pool-signature callers compare before/after |
| Provider-managed state | `provider_managed._mark_members_unadvertised`, `_persist_observed_config_locked`, five guarded branches in `execute_provider_operation`, `_record_discovery_revisioned`, `save_provider_configuration`, and `discover_provider_configs` | Fence provider member/config, discovery, mutation, and handoff operations; source contains conditional/CAS paths |
| Routing intent | `server_global_selection.set_global_fixed_server`, `clear_global_fixed_server`, `_restore_global_routing_state`; `vpn_auto_exclusive.save_vpn_auto_exclusive_source_ref`; `apply_orchestrator_commits._commit_global_server_mode` | Fence changed global fixed/Auto/exclusive/server-mode intent |
| Runtime/generation | `mihomo_reconcile.reconcile_mihomo_selective_default_fast`, `promote_mihomo_candidate_config`, `restore_mihomo_reconcile_checkpoint`, `reconcile_mihomo_runtime`; `mihomo_runtime.restart_mihomo_container`; `xray_subscription_service._restore_xray_generation_checkpoint` | Fence config promotion, container restart, generation, or rollback even when active logical server/provenance is unchanged |
| Import/maintenance | `control_plane_transfer_import.import_control_plane_snapshot`; `database_admin.cleanup_runtime_state` | Fence imported state or owned cleanup |

This inventory describes source paths, not proof that each path ran in the audited interval. The provider-member list is part of `selection_pool_signature()`, but the predeploy/current signatures and provider member identity/revision match; no observed provider-pool transition explains these 28 increments.

## Attributed revision timeline

The staged Xray path calls `_apply_staged_mihomo_candidate()` for transition and final candidates. For a changed candidate it calls unfenced `promote_mihomo_candidate_config()` and then unfenced `restart_mihomo_container(action="force_recreate")`. Each call advances the revision; the restart receipt is carried into the Xray generation/job result. A candidate already matching the active config returns `unchanged` before either operation.

The 8 retained successful refresh job receipts show contiguous numeric revisions 44–65. Promotion event timestamps and job IDs identify 11 successful candidate promotions; each promotion is paired with the promote and restart revision values shown below.

| Job created (UTC in SQLite) | Promotion event times (UTC) | Numeric fence revisions | Classification |
|---|---|---|---|
| 2026-10-06 00:00:05 | 00:00:42, 00:00:45 | 44–45; 46–47 | 4 observed generation fences |
| 2026-10-06 04:01:09 | 04:01:46 | 48–49 | 2 observed generation fences |
| 2026-10-06 08:00:01 | 08:00:36 | 50–51 | 2 observed generation fences |
| 2026-10-06 12:03:08 | 12:03:45 | 52–53 | 2 observed generation fences |
| 2026-10-06 16:00:30 | 16:01:08 | 54–55 | 2 observed generation fences |
| 2026-10-06 20:01:28 | 20:02:01, 20:02:04 | 56–57; 58–59 | 4 observed generation fences |
| 2026-10-07 00:01:58 | 00:02:36 | 60–61 | 2 observed generation fences |
| 2026-10-07 04:04:30 | 04:05:03, 04:05:06 | 62–63; 64–65 | 4 observed generation fences |

The first three promotion events occurred at 2026-10-05 12:03:51Z, 16:04:10Z, and 20:00:55Z. Each shares a workflow correlation with a `subscription_refresh_applied` event. The currently loaded timer and source unit both specify `OnCalendar=*-*-* 03,07,11,15,19,23:00:00` in the host's +07 timezone; the three event times align with scheduled runs at 19:03, 23:04, and next-day 03:00 local. No job rows for these three runs remain in the current jobs table. The predeploy value 37 and first retained numeric receipt 44 leave exactly six increments (38–43); source executes two fences per successful promotion, so the three promotion events account for those six in aggregate. **Numeric values 38–43 are inferred, not observed receipts, and are not assigned individually to the three events.**

## Selection oscillation and background overlap

Across the acceptance interval, 75 `logical_effective_member_changed` records group into 13 hashed logical identities. The two repeated-alternation groups contain 60 and 4 events. Both are ordinary subscription logical servers with Mihomo proxy-group type `fallback`, `source_defined` policy, and respectively 15 and 16 VLESS members. They have no provider binding. One has `vpn_auto=1` in ordinary preferences, but both are outside the currently exclusive Provider candidate pool and neither is the active global logical server. Current and predeploy selected identity, provenance, and sole-candidate pool signature match.

All 75 records are `component=health-runtime`, `operation=effective_member_observation`, `evidence_source=runtime_native`, and `adapter_id=mihomo`. The adapter reads Mihomo's current proxy-group `now` value; this event path observes the selected member and records the changed local projection. The groups are `fallback`, not `url-test`. The records establish repeated runtime-observed member alternation, not a global VPN-auto selection oscillation. No matching watchdog/recovery or explicit member-reselection job/event was retained. Some observations are within five minutes of refresh promotions; this is temporal overlap only and does not establish that refresh/restart caused them. The exact native fallback trigger remains unknown.

One `runtime_convergence_repaired` event at 2026-10-07 07:10:54Z was requested by `runtime_convergence_scheduler`; its retained details report `nftset_probe_unhealthy`, no dataplane drift/action, no rules/DHCP-DNS changes, and a successful selective check. No job/apply association or revision change is evidenced. This classifies a scheduled DNS convergence recovery, not its full cause.

Current Xray materialization is 71 bindings / 9 listeners versus 78 / 10 predeploy. The successful 2026-10-07 04:04:30 subscription job and 04:05:09 generation receipt support ordinary subscription refresh as the cause of the materialization difference. Provider binding remains member 1456, Hysteria2, binding/applied revision 4/4, exclusive routing and subscription provenance unchanged.

## Cost and provider evidence

An isolated replay of the actual `advance_selection_revision()` helper used a fresh disposable SQLite WAL database and 28 commits. It measured 28 `BEGIN IMMEDIATE` transactions, 28 settings upserts, and 25.978 ms for the loop. The final DB was 4,096 bytes and uncheckpointed WAL was 131,872 bytes; this WAL total includes initial schema creation because no pre-loop WAL baseline was captured, so it is not a fence-only WAL delta. Provider/runtime calls were zero. The reproducible replay is [replay_revision_wal.py](replay_revision_wal.py). This is a small isolated SQLite result, not a production latency or physical SSD-write estimate. Production physical-write attribution is not measurable retrospectively from the retained counters.

The existing same-process acceptance bracket already covered serial Health, router-summary, selector dry GET (`check_on_demand=false`, `update_ping_state=false`) and read-only UI activity: provider counters remained at 11 targeted `GET /configs`, with zero delta; discovery, mutation, error, rate-limit, and timeout counts remained zero. This audit did not repeat the bracket or contact the provider.

## Evidence boundary

Source and deployment code parity for the five attribution files is verified by SHA-256. Production SQLite was opened with URI `mode=ro`; the predeploy database was only read from the protected backup. Historical logs/jobs contain no revision event stream. The 22 retained numeric increments are directly evidenced by job results. The remaining six are aggregate source/event attribution with a known retention gap. No product test cohort, deployment, restart, runtime command, provider PATCH, probe, subscription refresh, or mutation was run by this audit. Production WAL bytes, device writes, runtime restart cost, and per-event numeric receipts for revisions 38–43 remain unmeasured/unavailable.


## Architectural review and no-fix decision

The Core selection contract is unchanged. Revision is a monotonic fence for selection inputs and runtime-generation changes, not a count of actual logical-target transitions. Changed staged candidates legitimately execute promotion and restart; unchanged candidates already short-circuit before either. Separate fence increments do not themselves prove a redundant runtime operation. No confirmed expensive no-op or policy-bypassing execution has been measured here, so no fence consolidation, scheduling change, probe suppression or routing change is made.

`logical_topology` also contains an unconditional same-member projection UPDATE in its runtime observation path. That is a source-level candidate for future attribution; refreshing observation/health state can be intentional, and its production call count, write volume and safe no-op semantics are not established. It is not a writer of the Auto revision. No optimization is authorized from this finding alone.

The prior acceptance artifact retains272 successful subject-inventory jobs,1074 traffic-accounting jobs,8 refresh jobs and1 retention-cleanup job since deploy, with no active job/apply and no new apply-version rows at capture. These counts do not equal scheduler wakeups and are not evidence that their overlap caused latency, revision increments or SSD writes. Refresh promotion timestamps align with the loaded four-hour schedule. Timing overlap with member observations is correlation only.

Per-increment classification is in [TIMELINE.json](TIMELINE.json):22 expected/observed and6 expected/inferred; no redundant or suspicious bump is established. The6 individual timestamps and operation assignments remain unavailable. This closes the numerical/source explanation of the aggregate increase, not the missing historical evidence gate or the native fallback trigger investigation.

## Validation levels and delivery boundary

L0: JSON syntax, Python AST syntax, timeline continuity/count assertions, secret-pattern scan and whitespace/source-surface checks. Isolated white-box (L1-style) measurement reuses the actual revision helper in disposable SQLite; it is not a product regression suite or deployment gate. No application behavior changes require new L2–L5 cohorts; retained contract evidence remains linked through the previous acceptance. No L6/L7, unrelated baseline rerun, application deploy/restart or production mutation. Before/after production comparison is not applicable because no fix is applied.
