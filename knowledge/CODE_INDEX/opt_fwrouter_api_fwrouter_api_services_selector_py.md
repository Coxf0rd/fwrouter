# `/opt/fwrouter-api/fwrouter_api/services/selector.py`

## Назначение

Выбирает лучший `vpn-auto` сервер на основе inventory, priority, cached ping state и optional on-demand checks.

`vpn_auto` membership шире, чем `Mihomo` auto-selection contract:
- кандидаты с `vpn_auto_priority >= 0` считаются `auto_selectable` и участвуют в `Mihomo/watchdog` автоматике;
- кандидаты с `vpn_auto_priority < 0` остаются в inventory/диагностике, но не считаются обязательными target-ами `vpn-auto` selector.
- custom proxy может быть членом `vpn-auto` для ручного выбора и Xray users. Selector сравнивает его с Mihomo inventory по `server_name`/`mihomo_target`, но persisted state и API остаются на `server_id`.

## Важные функции

- `_load_selector_candidates()`
- `_auto_selectable_candidates()`
- `get_vpn_auto_state()`
- `_candidate_with_on_demand_ping(...)`
- `_build_on_demand_shortlist(...)`
- `_select_best_successful_candidate(...)`
- `select_vpn_auto_server(...)`

## Внешние зависимости

- DB
- Mihomo adapter
- server ping service

## Runtime/persistent state

- может обновлять `routing_global_state.active_auto_server_id`
- может переключать live Mihomo selector
- successful apply writes operational log with `requested_by` and selected server details
- при apply для custom proxy в Mihomo отправляется target name, а в `routing_global_state` сохраняется persistent `server_id`

## Boot persistence relevance

Средняя/высокая. Active auto server влияет на фактический egress после boot.
