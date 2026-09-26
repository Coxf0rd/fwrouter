# Database Schema

## Overview

The canonical database is SQLite at `/var/lib/fwrouter-v2/fwrouter.db`. It stores persistent intent, operational metadata, jobs, logs, traffic accounting, module state, subject inventory, and reboot-surviving control-plane state.

Live nftables, `ip rule`, and `ip route` state are not stored as source of truth. They are reconstructed from database intent and generated/last-good artifacts during startup recovery.

## Lifecycle

- schema source: `/opt/fwrouter-api/fwrouter_api/db/schema.sql`
- runtime access: `/opt/fwrouter-api/fwrouter_api/db/connection.py`
- migration runner: `/opt/fwrouter-api/fwrouter_api/db/migrations.py`
- schema drift checks: `/opt/fwrouter-api/fwrouter_api/db/schema_state.py`
- current expected schema version: `21`
- SQLite modes: `journal_mode=WAL`, `synchronous=NORMAL`, `foreign_keys=ON`, `busy_timeout=30000`

## Table Domains

### Meta And Settings

- `schema_meta`: schema version and schema-level markers.
- `settings`: JSON key/value control-plane settings, including bypass/runtime options and compatibility display settings.
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

### Routing And Runtime

Routing state tables track global mode intent, apply status, artifacts, selector state, and runtime summary inputs. If live dataplane is enforced but module state says `not_configured`, that is drift and must be normalized or explicitly reported.

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

## Migration Rules

- Schema upgrades use explicit sequential migrations `N -> N+1`; the runner applies only missing versions and updates `schema_meta.schema_version` after each successful step.
- Maintenance/delete paths that remove parent rows must use the shared DB connection with `PRAGMA foreign_keys=ON`; otherwise current schema actions like `apply_versions.job_id -> jobs.job_id ON DELETE SET NULL` and `subscription_clients.account_id -> subscription_accounts.account_id ON DELETE CASCADE` will not fire.
- Fresh DB bootstrap creates current schema/version directly from `schema.sql`.
- Keep migrations deterministic and data-preserving.
- Preserve persistent intent across schema upgrades.
- Keep legacy/backfill logic in the migration that introduced the corresponding schema transition.
- Do not infer desired state from live kernel state.
- Do not create provider-specific module/connection/subject rows only because a provider capability exists or was discovered at runtime.
- Add tests for new schema state and repository helpers.
- Keep `schema_meta.schema_version` synchronized with `schema.sql` and schema checks.
