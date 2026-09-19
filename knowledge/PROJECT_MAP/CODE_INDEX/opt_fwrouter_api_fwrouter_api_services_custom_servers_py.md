# `/opt/fwrouter-api/fwrouter_api/services/custom_servers.py`

## Purpose

Owns user-created custom HTTPS/SOCKS proxy servers stored as persistent
server inventory plus `server_custom_https_proxy` endpoint details.

## Important Functions

- `create_custom_https_proxy_server(...)`
- `update_custom_https_proxy_server(...)`
- `delete_custom_https_proxy_server(...)`
- `resolve_runtime_proxy_rows(...)`

Custom proxy runtime rows use the operator-facing `server_name` as the Mihomo
proxy target while API/DB references stay on the stable `custom-https:*`
`server_id`.

## Runtime Impact

Writes `servers`, `server_preferences`, `server_ping_state`, and
`server_custom_https_proxy`. Create/update/delete can trigger Mihomo/Xray
reconcile when the custom server is or was included in `vpn_auto` or
`global_list`.

Custom servers are user-owned persistent entities. Subscription refresh,
membership cleanup, and subscription identity churn must not delete them,
rewrite their stable IDs, reset preferences, clear ping state, or create
duplicates.

Custom server create/update keeps custom proxies manual-only inside VPN-auto:
when `vpn_auto=true`, the server is persisted with `vpn_auto_priority=-1` and
manual origin. It remains visible in VPN-auto/global/manual target lists and is
materialized into Mihomo, but selector/watchdog and automatic Xray pools must
not choose it automatically.

## Guardrails

- Keep FWRouter core as the authority for classification and policy routing.
- Keep Mihomo as a VPN egress adapter, not the network policy engine.
- Do not treat subscription membership as ownership for custom servers.
- Do not confuse custom `server_id` with Mihomo runtime `server_name`.
