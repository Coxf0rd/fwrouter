# `/opt/fwrouter-api/fwrouter_api/services/external_provider_registry.py`

## Purpose

Provider capability registry for optional external integrations. It stores provider-specific parser/probe contracts, while concrete connection instances remain in the persistent `external_connections` registry.

## Important Functions

- `external_ingress_provider_contracts()`
- `external_ingress_provider_contract(provider)`
- `explicit_external_client_provider_contracts()`

## Runtime Impact

No runtime state by itself. Entries here describe available capabilities only; they must not create subjects, collectors, cache entries, generated configs, or persistent connection instances.

## Guardrails

- Keep concrete provider fields here or in provider adapters/parsers, not in generic subject taxonomy.
- Generic runtime code must still require `connection_id` before creating or probing instance-specific state.
