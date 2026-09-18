# `/opt/fwrouter-api/fwrouter_api/services/mihomo_config_proxies.py`

## Назначение

Runtime proxy inventory merge и сборка Mihomo selector groups (`vpn-auto`, `vpn-global`, subject-specific selectors).

## Runtime/persistent state

- читает runtime proxy inventory из `custom_servers`
- читает server preferences для `vpn-auto`
- state не пишет

## Нюансы

- Deduplicate proxy names before writing candidate config.
- `vpn-global` всегда содержит `vpn-auto` и заканчивается `DIRECT`.
- Subject selectors добавляются только для активных subject server overrides.
- `_merge_runtime_proxies(..., required_last_good_names=...)` can retain a
  narrowly scoped proxy definition from the active config for an applied Xray
  handoff while a subscription candidate is in flight. It never treats that
  retained proxy as active inventory and final reconciliation removes it once
  Xray has converged.
