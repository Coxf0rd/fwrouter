# `/opt/fwrouter-api/fwrouter_api/services/subscription_profiles.py`

## Назначение

Строит публичные subscription profiles для Xray/VLESS клиентов в разных форматах.

## Важные функции

- `resolve_subscription_client(...)`
- `build_subscription_nodes(...)`
- `list_desired_subscription_xray_clients(...)`
- `render_raw_vless_subscription(...)`
- `render_base64_vless_subscription(...)`
- `render_happ_subscription(...)`
- `render_clash_subscription(...)`
- `render_subscription_profile(...)`

## Внешние зависимости

- SQLite `subscription_accounts`, `subscription_clients`, `servers`, `server_preferences`
- `services/custom_servers.py` virtual Xray VPN-auto constants
- `services/xray_subscription.py`

## Runtime/persistent state

- создает/обновляет legacy subscription identity при resolution
- обновляет `subscription_clients.last_seen_at` и metadata при fetch
- не должен напрямую менять live Xray config; desired clients затем materialize-ятся через Xray sync/bindings

## Boot persistence relevance

Высокая для Xray subscription continuity. Tokens/accounts должны переживать reboot и сохранять stable UUID/email для generated VLESS nodes.

## Нюансы

- Формат выбирается по query/app/user-agent: Clash/Mihomo, raw/base64 VLESS, Happ.
- `last_seen_at` subscription client является UI activity signal за 24 часа.
- Stable UUID/email строятся детерминированно из token + server id.

