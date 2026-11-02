# `/opt/fwrouter-api/fwrouter_api/services/database_schema_read_model.py`

## Purpose

Owns the cached health/read projection of SQLite schema state. The 30-second
TTL cache remains in the existing live-probe cache service; this module derives
a key from the DB path, file identity/change metadata, SQLite schema cookie, and
schema version, then loads through read-only schema inspection.

## Runtime Impact

This is an observational projection. It opens an existing database with
`mode=ro`; it does not create a missing database, change journal mode, or own
schema initialization. Full schema admission is performed by
`db/schema_state.py` and normal writer opens validate on their own handle.

## Guardrails

- Keep this service above `db/connection.py` in the dependency direction.
- Preserve the existing cache TTL and clear/invalidation owner.
- Do not cache schema compatibility in the DB connection layer.
