# `/opt/fwrouter-api/fwrouter_api/db/migrations.py`

## Purpose

Versioned SQLite migration runner for FWRouter DB. It owns the explicit
`N -> N+1` transition chain from supported legacy schema versions to the
current schema.

## Important Functions

- `run_missing_migrations(connection)`
  Reads `schema_meta.schema_version`, applies only missing migrations in order,
  and updates the version marker after each successful step.
- migration functions `7 -> 8`, `8 -> 9`, `9 -> 10`, `10 -> 11`, `11 -> 12`,
  `12 -> 13`, `13 -> 14`
  Contain historical DDL/backfill/rebuild steps.

## Schema 12 -> 13

Migration `12 -> 13` moves subscription server identity away from display
names:

- drops the unique `server_name` index and recreates it as non-unique;
- creates `subscription_server_memberships`;
- recalculates recoverable subscription server IDs as `sub:<hash>`;
- preserves/moves `server_preferences`, `server_ping_state`,
  `subject_server_overrides`, and `routing_global_state` fixed/active refs;
- leaves custom proxy IDs unchanged;
- is tolerant of minimal legacy test DBs where `servers` has not been created
  yet, because fresh `schema.sql` bootstrap runs after migrations.

## Schema 13 -> 14

Migration `13 -> 14` adds `server_preferences.vpn_auto_priority_origin`.
Existing rows default to `legacy`; new code sets `auto` for backend-assigned
VPN-auto default priority and `manual` for explicit operator priority edits.

## Runtime/Persistent State

- Updates `/var/lib/fwrouter-v2/fwrouter.db`.
- Preserves existing user intent/data during schema upgrade.
- Migrates legacy provider detail tables into `subjects.metadata_json.detail`.

## Guardrails

- Fresh DB bootstrap does not run legacy migrations; `schema.sql` creates the
  current schema/version directly.
- Runtime/discovered artifacts must not become persistent user intent.
- Repeated startup must not rerun already applied migrations.
- Regression coverage checks the official initialization path, `PRAGMA
  integrity_check`, `PRAGMA foreign_key_check`, preservation of routing/server
  references, custom proxy IDs, memberships, and idempotent repeated bootstrap.
