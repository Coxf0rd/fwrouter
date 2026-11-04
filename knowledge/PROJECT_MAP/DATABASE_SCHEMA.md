# Database Schema

## Overview

The canonical database is SQLite at `/var/lib/fwrouter-v2/fwrouter.db`. It stores persistent intent, operational metadata, jobs, logs, traffic accounting, module state, subject inventory, and reboot-surviving control-plane state.

Live nftables, `ip rule`, and `ip route` state are not stored as source of truth. They are reconstructed from database intent and generated/last-good artifacts during startup recovery.

## Lifecycle

- schema source: `/opt/fwrouter-api/fwrouter_api/db/schema.sql`
- runtime access: `/opt/fwrouter-api/fwrouter_api/db/connection.py`
- complete schema admission: `/opt/fwrouter-api/fwrouter_api/db/schema_state.py`
- schema drift checks: `/opt/fwrouter-api/fwrouter_api/db/schema_state.py`
- source schema support floor: exact schema `27`; older versions are retained intact and rejected for operator-managed recovery
- initialization owner: API bootstrap and an explicitly admitted private rebuild candidate only
- SQLite modes: `journal_mode=WAL`, `synchronous=NORMAL`, `foreign_keys=ON`, `busy_timeout=30000`

## Table Domains

### Meta And Settings

- `schema_meta`: schema version and schema-level markers.
- `settings`: JSON key/value control-plane settings, including bypass/runtime options and compatibility display settings.
- `settings.key='core.intent_revision'`: local monotonic Core intent fence. It is initialized at zero, is never imported from another control plane, and advances atomically with Core desired/lifecycle/bypass/import writes. Malformed or missing values block Core apply completion.
- `external_connections`: persistent registry for `external_management`, `external_vpn_module`, `external_network_source`, and `display_only` connections. `connection_id` is the primary key, `system_id` is a non-unique compatibility/display identifier, and `value_json` stores the normalized connection contract.
- `external_connection_generated_state`: generated/per-connection state keyed by `connection_id`; rows are cascade-deleted with their connection.
- `external_connection_migrations`: one-time external-registry migration markers for explicit external-registry compatibility moves. Provider discovery does not promote runtime subjects into persistent connection instances.
- `modules`: desired/runtime/apply state plus `lifecycle_mode`. Clean DB seeds only core FWRouter rows (`core`, `vpn`, `watchdog`, `selector`, `subscription`). Optional provider/runtime rows such as `xray` and `tailscale` are not pre-created; they appear only after explicit user/API action or preserved migrated user state. `lifecycle_mode` is `none`, `managed`, or `external`; `ui` is intentionally not a runtime module.

### Subjects

- `subjects`: main subject inventory table with open `subject_type` for the concrete detail/runtime implementation, `subject_role` for generic grouping/policy/UI (`lan_client`, `external_network_source`, `vless_client`, `docker_runtime`, `host_runtime`, `router_core`), stable key, desired/applied modes, runtime state, lifecycle timestamps, active/deleted flags, and metadata JSON. `implementation_kind` stores the concrete adapter/provider value such as `tailscale` or `xray`; external-provider subjects store their owning `connection_id` and provider/client detail in metadata. DB schema must not hardcode provider values in a `subject_type` CHECK constraint.
- `subject_lan`: LAN client details, including MAC, IP, hostname, DHCP hostname, and source metadata.
- External-provider subject details are stored in `subjects.metadata_json.detail`; legacy `subject_tailscale` and `subject_xray` tables are migrated into metadata and dropped.
- `subject_docker`: Docker service/container details.
- `subject_host`: host/system service attribution details.
- `subject_fwrouter`: internal FWRouter component subjects.

All subjects remain in the shared inventory, but generic behavior must use `subject_role` where possible. Runtime/detail ownership still uses `subject_type`. Client-plane roles are `lan_client`, `external_network_source`, and `vless_client`. System/control roles are `host_runtime`, `docker_runtime`, and `router_core`.

### Policy And Overrides

- subject policy tables store desired per-subject modes and scoped egress configuration.
- LAN and Tailscale subjects participate in normal direct/selective/vpn decision flow.
- Xray clients are forced into VPN egress through their explicit Xray handoff/binding path.
- Host and Docker default to direct-safe behavior and require stable attribution before scoped VPN can be considered fully applied.
- `fwrouter:global` is not a user-facing implicit VPN subject. FWRouter own traffic stays direct-safe unless a future separate technical contour is introduced.

`subject_server_overrides` remains the owner of manual per-subject target intent,24-hour expiry and apply status. In schema27, `selected_target_kind` distinguishes a real `server` target from explicit `vpn_auto`. The latter stores SQL NULL in the physical `selected_server_id` foreign key and projects `virtual:xray:vpn-auto` in API/effective DTOs. A legacy NULL with kind `server` is not virtual intent. The foreign key stays enabled and no synthetic server row is created.

The historical additive 26→27 migration changed only this representation/schema marker. Schema upgrades are now retired; deployments with an older schema require a separately reviewed recovery/restore plan. Fresh schema enforces the allowed target kinds and virtual/NULL pairing. Native generation source fingerprints and scoped snapshots include target kind. Source verification/CI does not authorize changing a live database.

### Routing And Runtime

Routing state tables track global mode intent, apply status, artifacts, selector state, and runtime summary inputs. If live dataplane is enforced but module state says `not_configured`, that is drift and must be normalized or explicitly reported.

`apply_versions.core_intent_revision` (schema 26) binds a canonical global apply receipt to the local Core intent revision. It is nullable for pre-26 generations and those legacy rows never prove a current Core apply. A Core enable request remains pending until the existing global apply owner commits the exact current generation, then confirms fresh native enforcement in the same boot/network namespace and atomically settles that revision. Same-mode routing no-op does not settle Core. Explicit enable is required to renew an enabled legacy state with a NULL/stale binding; startup does not infer or manufacture proof.

Historically, the schema-25-to-26 migration ran its DDL, initial Core intent row, enabled-Core pending transition, and schema marker inside one SQLite savepoint. This historical transaction boundary did not repair or wrap other transitions. Current schema admission requires the complete schema-27 contract.

### Server Inventory And Subscription Membership

- `servers`: canonical server inventory. `server_id` is the primary key. For subscription servers it is a stable identity, not the display name. Duplicate `server_name` values are valid.
- `server_preferences`: vpn-auto/global-list participation, priority,
  auto/manual/legacy priority provenance, remembered/deleted state.
- `server_ping_state`: delay/probe result keyed by stable `server_id`. Runtime
  health uses the existing status/latency fields; manual presentation state is
  stored separately in `manual_*` columns so background failures do not erase
  the last manual observation.
- `server_custom_https_proxy`: custom proxy endpoint and optional credentials keyed by custom proxy `server_id`. Custom proxy IDs are not rewritten by subscription migrations.
- `subscription_server_memberships`: exact subscription source membership for stable subscription servers. Primary key is `(source_id, server_id)`. It stores `source_url`, `entry_identity_hash`, `parser_format`, `display_name`, timestamps, and `is_active`.
- `logical_server_topology`: normalized logical-server topology keyed by
  `logical_server_id`. Subscription-derived user-facing servers are logical
  servers even when they contain one concrete endpoint.
- `logical_server_members`: concrete members for each logical server. A
  single-endpoint server has one member; a structured profile has many.
  Reverse member_id/runtime_name indexes (2026-10-02) support canonical
  source-ownership lookup; these are additive indexes, schema remains 23.
- `logical_server_member_health`: provider-role-scoped member evidence with
  `unknown`, `healthy`, `failed`, `stale`, and `unsupported` states.
- `logical_server_group_probe_outcome`: latest explicit group-level probe result
  per logical server and provider role (`success`, `timeout`, `transport_error`,
  or `runtime_missing`), with checked timestamp, error, and adapter evidence.
- `logical_server_probe_state`: persisted cursor for bounded background member
  probing. The cursor survives backend restart and advances only across active
  server inventory; it is not a user selection or concrete-member pin.

One exact subscription entry appearing in two sources is one server plus two memberships. Removing an entry from one source does not mark the server missing while another active membership remains.

Subscription identity rules:

- flat URI/base64/plain-line entries use `server_id = sub:<sha256(exact trimmed URI)>`;
- structured YAML/JSON entries use `server_id = sub:<sha256(canonical raw semantic object)>`;
- display name is never identity;
- duplicate display names are allowed;
- Mihomo runtime proxy names are generated separately and must be unique.

### Jobs And Logs

Job tables store asynchronous apply/refresh/maintenance work, compact results, status, timestamps, artifact references, and a separate sanitized event-context snapshot used to correlate worker-thread events (never mixed into job input). Log envelopes include stable event IDs, UTC timestamps, severity, component, category/code and schema version. Migration `20 -> 21` scrubs credential-bearing operational SQLite rows and operational/technical JSONL records; JSONL rewrites are atomic and preserve record IDs/timestamps. All operational `created_at` values retain SQLite's sortable UTC format.

### Rules And Subscriptions

Rules tables store source metadata, update status, parsed domain/IP lists, generated artifacts, and refresh jobs. Subscription tables store public subscription clients, profile fetch metadata, Xray client mapping, source membership, and derived UI state.

### Traffic Accounting

Traffic tables store raw snapshots, computed deltas, monthly aggregates, and attribution data. Collectors return structured JSON; the backend owns persistence and aggregation.

## Current Schema Lifecycle

- Existing DB opens use SQLite `mode=rw` (writers) or `mode=ro` (observations), never create a missing file, and validate the complete schema-27 structure on that same handle before writer permission or WAL setup. This admits either the canonical fresh schema or one exact, baseline-derived legacy schema-27 profile; explicit initialization additionally runs `integrity_check` and `foreign_key_check` read-only. Ordinary opens do not run whole-file integrity checks.
- Schema version other than `27`, missing canonical objects, changed constraints outside an exact known profile, corruption, or unsupported structure fails closed. No upgrade, reset, reseed, or repair is attempted on an existing file.
- Only explicit bootstrap initialization or an admitted private rebuild candidate may create a missing database. It builds and validates the canonical schema only, in a private sibling, then publishes without replacing a concurrently created path. A concurrent published winner must pass existing-database structural, integrity, and foreign-key checks without being rewritten.
- Existing-schema inspection admits complete baseline profiles produced by the historical 7→27 and 10/11→27 routes. The allowlist compares the exact full object inventory and all canonical table semantics, with exact fingerprints for the only changed table set; it is not a generic missing-FK/CHECK exception. Both historical profiles lack the three `routing_global_state` foreign keys to `servers`; this compatibility label does not claim those databases enforce them. Inline CHECK placement and ALTER-added column order are retained as captured. Unknown hybrids continue to fail closed. Existing source/pinned backup validation can read these exact profiles without changing them; rebuild candidates remain canonical-only, and the exact source-to-candidate schema equality guard still refuses a legacy-shape rebuild.
- The exact `legacy_migrated_schema27_manual_ping_additive` profile additionally preserves historical 7→8 module/proxy and 13→15 preference/manual-ping ALTER declarations together. All canonical foreign keys remain; only the historical `manual_status` CHECK is absent. Admission requires the full four-table fingerprint combination and the unchanged remaining schema; an isolated missing CHECK or an unknown hybrid is rejected. This existing-file compatibility does not alter fresh canonical initialization or permit a legacy-shape rebuild.
- Repeated initialization of an existing schema-27 database is observational: intentional deleted seed rows and user data remain untouched.
- Additive non-unique performance indexes remain compatible, as they were explicitly admitted by the prior schema preflight. Additional unique indexes and other unexpected tables, views, triggers, or canonical-object changes are rejected.
- Writer opens use a process-local positive structural-admission LRU capped at 32 entries. Each open reads `schema_meta.schema_version` and hashes the ordered same-connection `sqlite_master` content inside a query-only SQLite transaction; the key also contains the canonical `schema.sql`/SQLite runtime identity. A cache hit skips repeated PRAGMA introspection only when the exact schema content and version were previously admitted. Invalid schemas are never cached. DDL/content changes produce a different key; the bounded cache is discarded on process restart. It does not cache data integrity, row state, or native proof.
- `external_connection_migrations` remains a domain-data migration ledger. Its purpose is separate from retired SQLite DDL/schema version transitions.
- `database upgrade --expected-schema 27` remains a verified, non-mutating current-schema check. Upgrade execution for schemas 24–26 and earlier is retired; old files remain intact for separately reviewed recovery.

## Historical Schema Transition Record

The following notes describe decisions and migrations that were implemented historically. They are not current upgrade instructions. Exact pre-retirement source is preserved in Git history; the Package E retirement audit records the supported transition list and consumer disposition.

- Historical schema upgrades used explicit sequential migrations `N -> N+1`; this runner and its transitions have been removed from current source.
- Historical schema 26 added the nullable Core revision binding and local revision setting. Migration 25→26 retained legacy apply rows as NULL, initialized the local counter without importing remote provenance, and marked enabled Core pending so an explicit canonical apply was required. Its old offline upgrade route is no longer an apply path.
- Maintenance/delete paths that remove parent rows must use the shared DB connection with `PRAGMA foreign_keys=ON`; otherwise current schema actions like `apply_versions.job_id -> jobs.job_id ON DELETE SET NULL` and `subscription_clients.account_id -> subscription_accounts.account_id ON DELETE CASCADE` will not fire.
- External subscription profile deletion waits for successful Xray runtime convergence before deleting the exact `subscription_accounts` row; FK cascade then removes owned `subscription_clients` and `subscription_profile_snapshots`. Failed convergence preserves those rows for retry.
- Fresh DB initialization creates current schema/version directly from `schema.sql` only when the target file is absent.
- Historical transitions aimed to preserve persistent intent; do not infer that old transition code remains supported.
- Historical backfill details remain in the source repository's Git history and existing dated knowledge records.
- Schema 21 scrubs known credential-bearing operational rows and JSONL logs; API startup repeats the idempotent configured JSONL scrub to cover installer/service environment path differences.
- Do not infer desired state from live kernel state.
- Do not create provider-specific module/connection/subject rows only because a provider capability exists or was discovered at runtime.
- Add tests for new schema state and repository helpers.
- Keep `schema_meta.schema_version` synchronized with `schema.sql` and schema checks.

## Provider-managed foundation — 2026-10-01

Schema 22 adds provider bindings, scoped member preferences/advertisements and bounded sanitized evidence. Provider-observed identity/protocol are separate from verified locally applied identity/protocol. Provider observation/evidence tables exclude keys and connection material. Schema 23 adds a dedicated private `provider_credentials` table, sanitized `provider_locations`, persisted safe resource choices, and removes the one-enabled-binding restriction. Migration bootstrap copies an environment key only to existing matching bindings, preserves stored keys and invalidates legacy verified revisions before source-scoped runtime IDs are applied. See [Provider-managed foundation](PROVIDER_MANAGED_FOUNDATION.md). Schema 23 migrated and live drift checks passed on 2026-10-01; the environment key bootstrapped only the existing matching binding. See the provider intent deployment report.

## Exclusive VPN-auto source intent — 2026-10-02

`settings.key = vpn_auto_exclusive_source_ref` stores `value_json = {"source_ref": "src:<stable hash>"}`. Absence means the ordinary global Auto pool. One row gives atomic replacement without per-server preference rewrites or a schema migration (schema remains 23). Exclusive source removal requires explicitly clearing/replacing that intent. The source reference controls global Auto eligibility only; fixed-target availability remains separate. See [Exclusive VPN-auto](EXCLUSIVE_VPN_AUTO.md).

## Automatic provider member-switch intent — 2026-10-05

Schema 24 adds `provider_bindings.allow_automatic_member_switch INTEGER NOT NULL DEFAULT 0 CHECK (... IN (0, 1))`. The additive 23→24 migration preserves credentials, current/applied member, material revisions and exclusive intent. Existing and new bindings default to manual-only member switching. Policy-only updates do not increment material `binding_revision`; recovery snapshots include the policy field for revalidation. No selection fence advance is needed for an ordinary configured membership edit that leaves the effective Auto pool unchanged.
