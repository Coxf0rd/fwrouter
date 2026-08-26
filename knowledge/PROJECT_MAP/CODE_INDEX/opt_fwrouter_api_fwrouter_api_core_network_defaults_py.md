# `/opt/fwrouter-api/fwrouter_api_core_network_defaults.py`

## Purpose

Single Python source for safe default values of the deployment network contract.

## Behavior Notes

- `core/config.py` uses these constants as Pydantic defaults.
- `services/network_contract.py` uses the same constants as fail-safe fallback for empty/invalid env values.
- Runtime deployment values belong in `/opt/fwrouter-api/.env`; this file only owns defaults, not host-local configuration.
