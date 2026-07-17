# `/opt/fwrouter-api/fwrouter_api/routes/ui.py`

## Назначение

Read-only и settings endpoints для UI read-model.

## Важные endpoints

- `GET /api/v2/ui/router-summary`
  Возвращает текущий global routing/server summary для user/admin UI.

- `GET /api/v2/ui/external-ip`
  Backend pair для user-view external IP display. Ходит с сервера на `https://api.ipify.org?format=json` напрямую и через Mihomo mixed proxy `http://127.0.0.1:5201`, парсит IPv4/IPv6 из JSON/text и возвращает `current_ip` + `vpn_ip`. `current_ip` соответствует обычному/current egress для сайтов вне VPN-списков; `vpn_ip` соответствует egress через выбранный VPN/proxy path. Старое поле `ip` сохранено как alias `current_ip`.

- `GET /api/v2/ui/clients`
  Возвращает display settings, полный clients read-model и отфильтрованный `panel_clients`.

- `GET /api/v2/ui/settings/workspace`
  Возвращает settings workspace read-model.

- `GET /api/v2/ui/settings/inventory`
  Lightweight inventory для settings/admin devices UI с фильтрами `kind`, `query`, `limit`.

- `GET/PUT /api/v2/ui/settings/display`
  Читает/сохраняет UI display settings, включая скрытие объектов из admin panel и traffic metric preferences.

## Внешние зависимости

- `services/ui_state.py`
- `httpx` для backend external IP fallback

## Runtime/persistent state

- read endpoints не меняют state
- `PUT /ui/settings/display` сохраняет UI display settings

## Нюансы

- `/ui/external-ip` показывает backend-observed current/VPN pair, а не гарантированно per-client scoped egress IP. Для user hero это практичный read-only indicator: current path берется напрямую, VPN path берется через локальный Mihomo mixed listener.
