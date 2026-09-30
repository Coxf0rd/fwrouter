# `fwrouter_api/services/manual_check.py`

Provider-neutral canonical health refresh for one logical server or a scoped
global sweep. The existing runtime adapter probe updates canonical member health;
the response rereads runtime-effective topology for the confirmed member latency.
It does not select servers or change routing/watchdog intent. Legacy manual Ping
storage remains a separate compatibility lane.

Global scopes are `admin_all`, `user_global`, and `user_vpn_auto`.
