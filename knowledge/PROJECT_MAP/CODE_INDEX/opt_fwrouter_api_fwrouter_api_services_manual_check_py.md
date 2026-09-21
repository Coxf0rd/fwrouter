# `fwrouter_api/services/manual_check.py`

Provider-neutral manual diagnostics for one logical server or a scoped global sweep. It enumerates active members, uses runtime adapter probes, persists only `server_ping_state.manual_*`, and never updates canonical runtime health, routing, selector, or watchdog state.

Global scopes are `admin_all`, `user_global`, and `user_vpn_auto`.
