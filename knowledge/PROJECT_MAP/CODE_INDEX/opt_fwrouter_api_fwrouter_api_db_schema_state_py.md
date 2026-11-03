# `/opt/fwrouter-api/fwrouter_api_db_schema_state.py`

## Purpose

Full SQLite schema-27 admission for existing FWRouter databases. The default
inspection compares against canonical `db/schema.sql` and admits only exact
baseline-derived legacy profiles for existing files. Those profiles combine
the complete canonical object/table checks with exact fingerprints for their
full known set of historical table deviations; arbitrary hybrids fail closed.
Canonical-only callers (fresh initialization, CLI current-schema verification,
and private rebuild candidates) opt out explicitly. Inspection never modifies
the database.

## Review Notes

Read the source file directly before changing related behavior. Check adjacent service, route, adapter, script, or systemd documentation as applicable.
Expected schema version is 27. Additive non-unique performance indexes remain
compatible; added unique indexes and other unexpected objects fail closed.
Known historical schema-27 profiles preserve missing routing foreign keys and
inline CHECK/ALTER-column declarations from old 7→27 and 10/11→27 routes; they
do not claim canonical constraints or mutate/repair existing files.
`subjects.subject_type` remains an open technical key, not a provider enum.
Health projections use a service-owned cache in
`services/database_schema_read_model.py`; low-level DB code has no service
cache import. `connection.py` also keeps a separate 32-entry positive
structural-admission cache keyed by same-connection SQLite schema content and
schema version; it does not cache data integrity or row state.

## Runtime Impact

This file is part of the FWRouter source/runtime surface. Keep this card synchronized when the file responsibility, runtime side effects, boot relevance, or risk profile changes.

## Guardrails

- Keep FWRouter core as the authority for classification and policy routing.
- Keep Mihomo as a VPN egress adapter, not the network policy engine.
- Preserve direct-safe behavior for host/control-plane traffic unless an explicit scoped contour says otherwise.
