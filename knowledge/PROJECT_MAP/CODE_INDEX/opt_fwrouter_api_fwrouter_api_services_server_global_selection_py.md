# `/opt/fwrouter-api/fwrouter_api/services/server_global_selection.py`

## Purpose

Owns global fixed-server and auto-server selection apply flows.

## Main Responsibilities

- Validate servers that can be selected for global VPN routing.
- Persist and clear global fixed-server intent.
- Apply fixed server targets to Mihomo and update routing state.
- Restore global auto mode and apply the `vpn-auto` selector path.

## Runtime Impact

Can call Mihomo selector APIs, reconcile routing state, and write operational
logs for global fixed/auto changes.

## Guardrails

- Keep persistent `server_id` separate from the Mihomo selector target.
- Preserve rollback behavior when runtime apply fails.
- Keep Xray VPN-auto virtual server handling explicit.
