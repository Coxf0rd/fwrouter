# `/opt/fwrouter-api/fwrouter_api/services/runtime_adapters.py`

## Purpose

Role-based runtime adapter registry for optional FWRouter integrations.

## Runtime Impact

Resolves the active adapter for `vpn_dataplane` and `explicit_client_runtime`
without making UI display IDs depend on concrete implementations. External
records come from `ui.admin_client_display.v1.custom_external_systems`; managed
fallbacks use the existing `vpn` and `xray` module rows.

## Guardrails

- Keep this module read-only: it selects adapters but must not create subjects,
  write runtime config, restart services, or mutate lifecycle state.
- External VPN dataplane support requires transparent redir/tproxy endpoints.
- External explicit-client runtime support is a contract/readiness layer; actual
  client create/delete/proxy behavior still belongs to a dedicated adapter.
