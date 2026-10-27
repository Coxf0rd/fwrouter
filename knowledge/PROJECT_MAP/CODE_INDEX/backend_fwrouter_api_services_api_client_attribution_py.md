# `backend/fwrouter_api/services/api_client_attribution.py`

## Owner and purpose

Bounded server-owned attribution context for existing event writers and job
context snapshots. Identity is supplied by the existing scoped presentation
admission, not HTTP claims. This module grants no authority and does not own
registry persistence, domain revisions, native apply or recovery.

## Contracts

- Server request identity is independent of compatible caller correlation.
- Credential-bound metadata is a bounded event-time snapshot; legacy capability
  possession does not identify an individual API client.
- Label derivation cannot change client kind or consult a renamed current tag.
- Reserved identity namespaces from arbitrary details cannot override context.
- Async context transports metadata, never a reusable grant.
- Existing unprivileged readers recursively remove the new identity namespace.
- Rejection telemetry uses bounded identity/reason windows and remains best effort;
  adopted intent audit is mandatory and atomic with its owner transaction.

See [the attribution contract](../../API_CLIENT_ATTRIBUTION.md) for coverage,
privacy, honest outcome semantics and activation limitations.
