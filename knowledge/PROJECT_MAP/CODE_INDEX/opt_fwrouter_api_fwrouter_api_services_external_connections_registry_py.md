# `/opt/fwrouter-api/fwrouter_api/services/external_connections_registry.py`

Owns the persistent registry for external connections.

Responsibilities:

- Lists, reads, upserts, and deletes rows in `external_connections`.
- Keeps the legacy `custom_external_systems` response shape compatible while using `connection_id` as the only runtime/API lookup key; `system_id` is non-unique display metadata.
- Creates and idempotently updates per-connection generated state in `external_connection_generated_state`.
- Rejects conflicting active `external_vpn_module` records for the same replacement target.
- Clears only live probe cache and collector scheduler state scoped to the changed/deleted `connection_id`.

Used by UI display settings, external collectors, runtime adapter selection, and tests that validate multi-instance external network sources.
