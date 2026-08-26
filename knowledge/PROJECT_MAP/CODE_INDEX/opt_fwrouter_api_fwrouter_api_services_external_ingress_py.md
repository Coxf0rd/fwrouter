# `/opt/fwrouter-api/fwrouter_api/services/external_ingress.py`

## Purpose

Generic adapter for external ingress providers. It runs read-only probes from registry contracts and normalizes provider payloads into generic external-network clients.

## Important Functions

- `probe_external_ingress_runtime(provider)`
- `external_ingress_clients_from_payload(provider, payload, include_all_peers=False)`
- `external_ingress_clients_from_script_result(provider, result, include_all_peers=False)`

## External Dependencies

- `subject_taxonomy.external_ingress_contract(...)`
- allowlisted script runner for `command_probe`

## Runtime Impact

- read-only runtime probe
- no persistent writes

## Guardrails

- Provider-specific field names belong in taxonomy contracts, not generic runtime/policy/apply code.
- Concrete providers live as registry contracts/presets in `subject_taxonomy.py`, not as standalone service modules.
