# `backend/fwrouter_api/services/ordinary_event_read_model.py`

## Purpose

Own the existing ordinary event-summary label lookup and domain projection,
separate from HTTP transport and secure-event admission. Introduced for Stage 6.1
S61-F06.

## Entry point

`project_recent_event_summary(events)` receives the existing category-grouped
ordinary event rows and returns the same compact audit/operational/diagnostic
projection. It performs the existing bounded server/subject/member label SELECTs
through `db_session` inside the context-local read-only session scope,
preserving SQL/query order, sanitization,
field allowlists, event order and snapshot/current/missing fallback.

It adds no cache, persistent state, configuration, event writer, generic
repository or framework. It depends on persistence and existing event sanitizers;
it has no dependency on HTTP routes or secure-event readers.

## Boundaries

- HTTP query validation, default/limit handling, retrieval delegation, response
  construction and final `redact_http_attribution` remain in the route.
- Full-view summary and exact event retrieval remain with `services.events`.
- Secure-event admission, registry capabilities, revoke/drain and serialization
  leases remain separate and unchanged.
- These label-only queries use SQLite `mode=ro`; unrelated service DB writers
  retain normal WAL/permission behavior.
- Maximum 1,000 IDs per entity kind and 400 logical member pairs are preserved.

## Verification ownership

`test_events_api.py` characterizes ordinary JSON/status/filter/order, label and
projection fallbacks, redaction and query bounds; event visibility/model and
secure-event suites guard the neighboring contracts. Source/local/CI validation
does not claim native, packet, external VPN or production acceptance.
