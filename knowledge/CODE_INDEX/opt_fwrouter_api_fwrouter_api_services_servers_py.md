# `/opt/fwrouter-api/fwrouter_api/services/servers.py`

## Назначение

Управляет inventory серверов, глобальным routing state, fixed server mode и subject-level server overrides.

## Важные функции

- `ensure_routing_global_state()`
  Гарантирует наличие canonical глобального routing state в SQLite.

- `get_routing_global_state()`
- `set_global_mode(...)`
- `set_global_fixed_server(...)`
  Persistent fixed server хранится по `server_id` с backend TTL 24 часа; runtime apply для custom proxy отправляет в Mihomo `server_name` target.
- `clear_global_fixed_server(...)`
- `expire_global_fixed_server(...)`
  Возвращает просроченный global fixed-server в `auto`; runtime-convergence применяет возврат к `vpn-auto`.
- `set_subject_server_override(...)`
- `clear_subject_server_override(...)`

## Внешние зависимости

- SQLite
- Mihomo inventory/selectors
- subject policy services

## Runtime/persistent state

- хранит intended state, который потом materialize'ится в live dataplane

## Boot persistence relevance

Высокая. Именно этот persisted intent должен пережить reboot.

## Нюансы

- нельзя путать `desired_*` и `applied_*` поля
- startup recovery ориентируется на persisted mode из этого слоя
- нельзя путать persistent `server_id` и Mihomo selector target. Для subscription servers они обычно совпадают, для custom proxy (`Proxy6`) нет.
