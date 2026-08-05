# Database Schema

## Overview

The canonical database is SQLite at `/var/lib/fwrouter-v2/fwrouter.db`. It stores persistent intent, operational metadata, jobs, logs, traffic accounting, module state, subject inventory, and reboot-surviving control-plane state.

Live nftables, `ip rule`, and `ip route` state are not stored as source of truth. They are reconstructed from database intent and generated/last-good artifacts during startup recovery.

## Lifecycle

- schema source: `/opt/fwrouter-api/fwrouter_api/db/schema.sql`
- runtime access: `/opt/fwrouter-api/fwrouter_api/db/connection.py`
- schema drift checks: `/opt/fwrouter-api/fwrouter_api/db/schema_state.py`
- current expected schema version: `8`
- SQLite modes: `journal_mode=WAL`, `synchronous=NORMAL`, `foreign_keys=ON`, `busy_timeout=30000`

## Table Domains

### Meta And Settings

- `schema_meta`: schema version and schema-level markers.
- `settings`: JSON key/value control-plane settings, including bypass and runtime options.
- `modules`: desired/runtime/apply state plus `lifecycle_mode` for modules such as `core`, `vpn`, `xray`, `tailscale`, `watchdog`, `selector`, and `subscription`. `lifecycle_mode` is `none`, `managed`, or `external`; `ui` is intentionally not a runtime module.

### Subjects

- `subjects`: main subject inventory table with type, stable key, desired/applied modes, runtime state, lifecycle timestamps, active/deleted flags, and metadata JSON.
- `subject_lan`: LAN client details, including MAC, IP, hostname, DHCP hostname, and source metadata.
- `subject_tailscale`: Tailscale node details, including node id, Tailscale IP, hostname, user, online state, and source metadata.
- `subject_xray`: Xray client details, including UUID, email, subscription path, last subscription time, and enabled state.
- `subject_docker`: Docker service/container details.
- `subject_host`: host/system service attribution details.
- `subject_fwrouter`: internal FWRouter component subjects.

All subject types remain in the shared inventory, but their routing roles differ. Client-plane subjects are `lan`, `tailscale_node`, and `xray`. System/control subjects are `host`, `docker`, and `fwrouter`.

### Policy And Overrides

- subject policy tables store desired per-subject modes and scoped egress configuration.
- LAN and Tailscale subjects participate in normal direct/selective/vpn decision flow.
- Xray clients are forced into VPN egress through their explicit Xray handoff/binding path.
- Host and Docker default to direct-safe behavior and require stable attribution before scoped VPN can be considered fully applied.
- `fwrouter:global` is not a user-facing implicit VPN subject. FWRouter own traffic stays direct-safe unless a future separate technical contour is introduced.

### Routing And Runtime

Routing state tables track global mode intent, apply status, artifacts, selector state, and runtime summary inputs. If live dataplane is enforced but module state says `not_configured`, that is drift and must be normalized or explicitly reported.

### Jobs And Logs

Job tables store asynchronous apply/refresh/maintenance work, compact results, status, timestamps, and artifact references. Log tables store operational and technical events with retention managed by maintenance.

### Rules And Subscriptions

Rules tables store source metadata, update status, parsed domain/IP lists, generated artifacts, and refresh jobs. Subscription tables store public subscription clients, profile fetch metadata, Xray client mapping, and derived UI state.

### Traffic Accounting

Traffic tables store raw snapshots, computed deltas, monthly aggregates, and attribution data. Collectors return structured JSON; the backend owns persistence and aggregation.

## Migration Rules

- Keep migrations deterministic and idempotent.
- Preserve persistent intent across schema upgrades.
- Do not infer desired state from live kernel state.
- Add tests for new schema state and repository helpers.
- Keep `schema_meta.schema_version` synchronized with `schema.sql` and schema checks.
