# `/opt/fwrouter-api/fwrouter_api/services/xray_status.py`

## Purpose

Builds the Xray status DTO split out from `xray.py`.

## Runtime Impact

Calls the Xray adapter health probe, checks required Mihomo handoff listener
ports, reads generated binding/config state through `xray_runtime_state.py`,
and updates the Xray module row status through that helper layer.

It also reports private generation checkpoint state and gates forced-VPN
readiness on exact managed UUID/email parity between the active VLESS inbound
and applied bindings/mode directives. Missing or extra managed identities are
drift; unavailable config evidence is unknown. This does not rewrite individual
client health.

## Guardrails

- Keep this module read/status-oriented; client CRUD, subscription export, and
  materialization orchestration belong in `xray.py`.
- Keep the status DTO shape compatible with `/api/v2/xray` and UI runtime
  summary callers.
