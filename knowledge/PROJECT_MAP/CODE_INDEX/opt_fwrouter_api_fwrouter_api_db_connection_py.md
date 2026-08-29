# `/opt/fwrouter-api/fwrouter_api_db_connection.py`

## Purpose

Generated code-index entry for `/opt/fwrouter-api/fwrouter_api_db_connection.py`.

## Review Notes

Read the source file directly before changing related behavior. Check adjacent service, route, adapter, script, or systemd documentation as applicable.

## Runtime Impact

This file is part of the FWRouter source/runtime surface. Keep this card synchronized when the file responsibility, runtime side effects, boot relevance, or risk profile changes.

`connect()` sets `busy_timeout` before `journal_mode=WAL` and retries transient
`database is locked` failures while enabling WAL. This keeps sequential
TestClient/job tests and startup paths from failing when a previous SQLite
connection is still releasing its lock.

`initialize_database()` is now bootstrap orchestration only: it runs the
versioned migration runner from `db/migrations.py` when an existing DB has an
older `schema_meta.schema_version`, then applies current `schema.sql` and
returns schema inspection state. Historical backfill/rebuild logic must stay in
the migration for the schema transition that introduced it, not in this module.

## Guardrails

- Keep FWRouter core as the authority for classification and policy routing.
- Keep Mihomo as a VPN egress adapter, not the network policy engine.
- Preserve direct-safe behavior for host/control-plane traffic unless an explicit scoped contour says otherwise.
