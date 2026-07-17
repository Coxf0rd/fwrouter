# Invariants

- install/setup scripts должны быть идемпотентными
- повторный `start/apply` не должен плодить дубликаты `ip rule`
- `ip route` в таблице `100` должен ставиться через replace/recreate, а не накоплением мусора
- owned `nftables` table должна пересоздаваться безопасно
- source of truth для desired routing это SQLite + generated artifacts, а не текущий live kernel state
- runtime state не должен храниться как persistent config
- backend не должен затирать intended selective/vpn mode в БД только потому, что после reboot live dataplane отсутствует
- `fwrouter-api.service` не должен стартовать раньше готовности Mihomo/Xray и минимального host preflight
- protected/private адреса и сервисные домены не должны попадать в proxy loop
- `src_valid_mark=1` обязателен для fwmark-based routing contract
- `fwmark 0x100`, bypass mark `0x200`, table id `100`, priority `100` нельзя менять частично
- `proxy_net` для Xray считается внешней зависимостью, пока проект не начал создавать его сам
