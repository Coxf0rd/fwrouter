# `/opt/fwrouter-api/fwrouter_api/services/runtime_adapters.py`

## Purpose

Role-based runtime adapter registry for optional FWRouter integrations.
Registrations map `role -> adapter_id` and declare capabilities, priority,
replacement targets, a read-only resolver, and an optional operations factory.

## Runtime Impact

Resolves the active adapter for `vpn_dataplane` and `explicit_client_runtime`
without making generic core code branch on provider names. External records
come from the persistent `external_connections` registry; managed fallbacks use
the existing `vpn` and `xray` module rows.

## Key Functions

- `register_runtime_adapter(...)`
- `registered_runtime_adapters(...)`
- `active_runtime_adapter(role)`
- `runtime_adapter_operations(adapter)`
- `runtime_role_for_replacement_target(target)`
- `active_vpn_dataplane_adapter()`
- `active_explicit_client_runtime_adapter()`

## Guardrails

- Keep this module read-only: it selects adapters but must not create subjects,
  write runtime config, restart services, or mutate lifecycle state.
- External VPN dataplane support requires transparent redir/tproxy endpoints.
- External explicit-client runtime support is a contract/readiness layer; actual
  client create/delete/proxy behavior still belongs to a dedicated adapter.
- Selector-capable adapters must declare `health`, `list_servers` and
  `apply_server`; selector restore additionally requires `apply_selector`.
