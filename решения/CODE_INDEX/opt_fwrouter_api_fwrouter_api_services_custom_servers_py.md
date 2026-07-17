# `/opt/fwrouter-api/fwrouter_api/services/custom_servers.py`

## Назначение

Управляет custom HTTPS/SOCKS proxy серверами, которые добавляются из UI и хранятся в `servers` + `server_custom_https_proxy`.

## Важные функции

- `create_custom_https_proxy_server(...)`
- `update_custom_https_proxy_server(...)`
- `delete_custom_https_proxy_server(...)`
  После изменения custom proxy запускают Mihomo/Xray reconcile, если proxy включен в `vpn_auto` или `global_list` сейчас либо был включен до изменения. Это нужно, чтобы UI-added proxy сразу появлялся в `vpn-auto`/`vpn-global` runtime selector targets и Xray vpn-auto subscription.

- `resolve_mihomo_runtime_proxy_rows(...)`
  Возвращает runtime proxy rows для generated Mihomo config. Для custom proxy `raw.name`/Mihomo target равен `server_name`, а persistent API id остается `server_id` вида `custom-https:*`.

## Внешние зависимости

- SQLite
- `services.servers` для inventory read-model и runtime reconcile

## Runtime/persistent state

- persistent: `servers`, `server_preferences`, `server_ping_state`, `server_custom_https_proxy`
- runtime: через reconcile обновляет Mihomo config/selectors и Xray vpn-auto subscription

## Boot persistence relevance

Средняя/высокая. Custom proxy preferences должны переживать reboot и после config regeneration оставаться доступными в runtime selectors.

## Нюансы

- `vpn_auto_priority=-1` означает "ручной target в `vpn-auto`, но не auto-selectable кандидат".
- Для custom proxy нельзя путать persistent `server_id` и Mihomo target `server_name`.
- Xray subscription может показывать custom proxy под отдельным operator-facing label; это не должно переименовывать сам server inventory.
