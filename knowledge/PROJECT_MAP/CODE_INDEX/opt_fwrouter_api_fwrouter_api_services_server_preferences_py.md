# `/opt/fwrouter-api/fwrouter_api/services/server_preferences.py`

## Purpose

Owns user-visible server preferences for VPN-auto and global-list membership.

## Main Responsibilities

- Update per-server `vpn_auto`, `vpn_auto_priority`, and `global_list` flags.
- Replace the full VPN-auto membership list.
- Reconcile Mihomo/Xray generated runtime config after membership changes.
- Trigger VPN-auto reselection when the active auto server becomes invalid.

## Runtime Impact

Writes SQLite preferences and can trigger Mihomo/Xray reconcile plus selector
reselection. It does not own global fixed-server state directly.

## Guardrails

- Keep the optional reconcile callback injectable for facade compatibility tests.
- Reselect VPN-auto only when membership changes invalidate the active auto server.
- Return concise summaries rather than full inventory payloads in preference results.
