# `/opt/fwrouter-api/fwrouter_api/services/xray_bindings.py`

## Purpose

Collects scoped Xray runtime bindings and writes the persistent binding state
artifact after the split from `xray.py`.

## Runtime Impact

Reads active Xray subjects, enriches them with effective routing state, maps
VPN-mode clients to concrete handoff targets, builds Mihomo handoff listener
metadata, and writes `/var/lib/fwrouter-v2/xray/fwrouter-bindings.json`.
Concrete server handoff targets use the Mihomo runtime proxy name supplied by
`xray_runtime_state.py`, not the display `server_name`.

## Guardrails

- Keep materialization orchestration and adapter reload calls in `xray.py`.
- Keep this module focused on binding DTO collection and state artifact writing.
- Do not persist raw server configs in the safe bindings state payload.
