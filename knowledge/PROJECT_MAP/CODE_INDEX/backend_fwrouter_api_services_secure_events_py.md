# Secure event reader and admission

Files:
- `backend/fwrouter_api/services/secure_events.py`
- `backend/fwrouter_api/services/secure_event_read_admission.py`
- `backend/fwrouter_api/routes/events.py`: `/events/secure/recent`

Owner: the existing root client registry authenticates and authorizes `events.read`;
existing SQLite operational event writers own recorded data. Read admission does
not grant presentation mutations, import or Core/runtime authority.

Contracts: registry-only/fail-closed admission; existing conservative transport
floor; shared policy lease through response materialization; bounded read-only
SQLite keyset traversal; fixed safe projection; historical identity snapshot;
legacy/unverified separation; no read-generated audit noise; no-cache responses.
Legacy HTTP projections and their recursive reserved-identity redaction remain
unchanged. No new store, schema, global revision, listener or proxy configuration.

See [the read contract](../../SECURE_EVENT_READING.md) for API bounds, cursor
semantics, field classification, provisioning and separate activation gates.
Qualification is isolated source/CI; production access and transport are not proven.
