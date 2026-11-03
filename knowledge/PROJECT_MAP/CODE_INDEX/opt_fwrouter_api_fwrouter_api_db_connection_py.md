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

`initialize_database()` creates schema 27 only when the database path is absent.
For an existing path it performs read-only schema inspection; it never migrates,
reseeds, repairs, resets, or replaces data. Bootstrap and an admitted private
rebuild candidate are the only fresh-database initialization owners. Historical
schema transitions were retired; see the Package E audit for the support floor
and retained history.

Every writer opens an existing file with SQLite `mode=rw` and validates the
complete canonical-or-exact-known-legacy schema contract on that same
connection before chmod or WAL setup. Fresh initialization remains
canonical-only.
Successful structural admission is cached process-locally by the exact ordered
`sqlite_master` content digest, current schema version, canonical schema source
identity, and SQLite runtime version. The positive LRU is bounded to 32 entries;
the signature and version are read on every connection in a query-only read
transaction. Invalid results are not cached. DDL changes create a new key and a
process restart clears the cache.
Observation uses `mode=ro`. Explicit initialization also checks SQLite
integrity and foreign keys without writing. Ordinary opens do not run whole-file
integrity checks. Reads and health never initialize or create a missing
database. Unsupported schema and inspection errors fail closed.
`existing_database_readonly_connection()` is the low-level raw existing-file
observation seam for health/diagnostics that must inspect an unsupported schema
without writer admission, chmod, or WAL setup; it never creates a missing file.
Health/runtime schema projections use the service-owned existing 30-second
cache; its identity includes the path/file metadata and SQLite schema cookie.

`readonly_db_session_scope()` is a context-local override for nested projection
reads during native proof capture. It opens existing databases with
`mode=ro`/`query_only`, never creates or chmods the database path, and never
changes journal mode. It resets on normal return or exception and does not
affect other threads/tasks. SQLite WAL readers may update shared-memory reader
bookkeeping; the guarantee is no durable database/config mutation, not a
bit-for-bit unchanged WAL/SHM snapshot.

## Guardrails

- Keep FWRouter core as the authority for classification and policy routing.
- Keep Mihomo as a VPN egress adapter, not the network policy engine.
- Preserve direct-safe behavior for host/control-plane traffic unless an explicit scoped contour says otherwise.
