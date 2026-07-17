# `/opt/fwrouter-api/fwrouter_api/services/xray.py`

## Назначение

Центральный service-layer для Xray: status, clients, subscriptions, runtime bindings и handoff assignments.

## Важные функции

- `_load_xray_bindings_state()`
  Читает persistent bindings state из `/var/lib/fwrouter-v2/xray/fwrouter-bindings.json`.

- `_xray_config_egress_summary()`
  Анализирует generated `config.json` и определяет, доступен ли реальный outbound.

- `_xray_materializable_egress_candidate()`
  Проверяет, можно ли построить Xray egress binding из текущего routing/server state.

- preflight и service-call entrypoints для:
  - `get_xray_status()`
  - `list_xray_clients()`
  - `create_xray_client()`
  - `reload_xray()`
  - `sync_xray_subjects()`
  - subscription export helpers

## Внешние зависимости

- Xray adapter
- Mihomo adapter
- DB
- subscription profile builders
- subject inventory/policy

## Runtime/persistent state

- читает и пишет Xray bindings/config-related artifacts
- может вызывать runtime reload Xray

## Boot persistence relevance

Средняя/высокая. Важен для client subscription plane и post-boot scoped bindings.

## Нюансы

- `xray` здесь тесно связан с global routing selection, хотя не владеет host dataplane
- поддерживаются только определенные server shapes для materialized egress
- public subscription profile и internal `vpn-auto`/profile runtime bindings это разные контуры, но виртуальный public node `Автоматический выбор` должен оставаться в обычной Xray subscription как стабильная точка входа на актуальный `vpn-global`
- custom proxy с `vpn_auto=1` включается в Xray vpn-auto subscription как отдельный ручной node, даже если `vpn_auto_priority=-1`; priority влияет на auto-selector, а не на доступность для Xray users. Для `Proxy6` Xray-only label намеренно остается `Proxy (не заходить)` и не должен переименовывать основной server inventory/Mihomo target.
- Xray subject server override materialization не должна требовать отдельного host dataplane verify; для `xray` это control-plane/runtime metadata path
