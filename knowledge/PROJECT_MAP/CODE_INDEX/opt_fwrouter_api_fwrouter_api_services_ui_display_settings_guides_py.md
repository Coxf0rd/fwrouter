# `/opt/fwrouter-api/fwrouter_api/services/ui_display_settings_guides.py`

## Purpose

Builds API guide and readiness metadata for external management, external VPN module, and external network source connections.

## Important Functions

- `_external_connection_guide(...)`
- `_external_management_api_guide(...)`
- `_external_vpn_module_guide(...)`
- `_external_network_source_guide(...)`
- `_external_collection_guide(...)`
- `_external_connection_readiness(...)`

## Notes

- Guide payloads are machine-readable contracts; UI-facing labels still belong in `ui_text.py`.
- External VPN module readiness accounts for the active runtime adapter by replacement target.
