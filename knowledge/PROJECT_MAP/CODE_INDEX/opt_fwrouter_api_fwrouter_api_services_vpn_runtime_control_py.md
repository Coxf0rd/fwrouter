# `/opt/fwrouter-api/fwrouter_api/services/vpn_runtime_control.py`

## Purpose

Generic VPN runtime controller boundary used by watchdog for
provider-independent runtime state, active-target probing, initial selection
and failover.

## Key Classes

- `VpnRuntimeController`
  Base controller for runtimes without a failover API. It returns generic
  `path_key`, selection mode, active target, capability flags and structured
  unsupported failover/initial-selection results.
- `MihomoVpnRuntimeController`
  Adapts existing Mihomo selector and active-server delay checks to the generic
  watchdog runtime contract.
- `ExternalVpnRuntimeController`
  Uses explicit external selector endpoints only when
  `capabilities.supports_selector_api=true` and both selector endpoint URLs are
  present in the external connection contract.

## Key Functions

- `get_vpn_runtime_controller(...)`
  Selects the controller for the active `vpn_dataplane` adapter id. Watchdog
  decision logic should depend on this generic boundary instead of importing
  provider-specific selector/probe functions directly.

## Runtime Impact

Does not own runtime lifecycle. Mihomo paths call the existing selector/probe
layer. External VPN paths call only user-configured HTTP endpoints from the
external connection contract.

## Guardrails

- Keep `path_key` stable enough for watchdog candidate reset semantics.
- External selector API requires both `selector_state_url` and
  `selector_failover_url`; do not infer support from one endpoint.
- Keep provider-specific behavior behind controller classes, not inside core
  watchdog flow modules.
