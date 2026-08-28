# `/opt/fwrouter-api/fwrouter_api/services/external_ingress.py`

## Purpose

Generic adapter for external ingress providers. It runs read-only probes from provider registry contracts and normalizes provider payloads into generic external-network clients.

## Important Functions

- `probe_external_ingress_runtime(provider, connection_id=..., collector_config=...)`
- `external_ingress_clients_from_payload(provider, payload, connection_id=..., include_all_peers=False)`
- `external_ingress_clients_from_script_result(provider, result, connection_id=..., include_all_peers=False)`

## External Dependencies

- `subject_taxonomy.external_ingress_contract(...)`
- `external_provider_registry` for provider-specific parser/probe contracts
- allowlisted script runner for `command_probe`

## Runtime Impact

- read-only runtime probe; probes require a concrete `connection_id` and probe cache keys are scoped to that connection
- no persistent writes

## Guardrails

- Provider-specific field names belong in provider registry contracts or provider adapters, not generic runtime/policy/apply code.
- Concrete providers live as registry contracts/presets in `external_provider_registry.py`, not as generic subject types.
- Callers that create subjects must pass a concrete `connection_id`; provider capability alone is not a runtime instance.
