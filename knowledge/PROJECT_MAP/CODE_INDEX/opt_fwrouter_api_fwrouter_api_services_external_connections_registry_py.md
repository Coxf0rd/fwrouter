# `/opt/fwrouter-api/fwrouter_api/services/external_connections_registry.py`

Owns the persistent registry for external connections.

Responsibilities:

- Lists, reads, upserts, and deletes rows in `external_connections`.
- Keeps the legacy `system_id`/`custom_external_systems` API shape compatible while using `connection_id` as the stable registry key.
- Creates and updates per-connection generated state in `external_connection_generated_state`.
- Rejects conflicting active `external_vpn_module` records for the same replacement target.
- Clears live probe cache after connection or generated-state changes.

Used by UI display settings, external collectors, runtime adapter selection, and tests that validate multi-instance external network sources.
