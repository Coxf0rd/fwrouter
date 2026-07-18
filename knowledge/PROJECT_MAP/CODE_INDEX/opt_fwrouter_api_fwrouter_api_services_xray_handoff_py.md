# `/opt/fwrouter-api/fwrouter_api/services/xray_handoff.py`

## Назначение

Строит deterministic Xray -> Mihomo handoff assignments для selected server bindings.

## Важные функции

- `xray_handoff_digest(...)`
- `xray_managed_egress_tag(...)`
- `xray_mihomo_listener_name(...)`
- `build_xray_handoff_assignments(...)`

## Внешние зависимости

Нет внешних сервисов; вызывается config/building code.

## Runtime/persistent state

State не пишет. Возвращает tag/listener/port assignments.

## Boot persistence relevance

Средняя/высокая. Stable handoff tags/ports нужны, чтобы Xray config и Mihomo listener config совпадали после regeneration/reboot.

## Нюансы

- Port range: `53100..63099` на host `172.18.0.1`.
- Collision resolution линейно ищет свободный port; при полном диапазоне бросает error.
- Assignment group key это `selected_server_id`.

