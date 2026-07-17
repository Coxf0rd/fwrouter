# `/opt/fwrouter-api/fwrouter_api/core/config.py`

## Назначение

Runtime settings backend-а через Pydantic settings и `FWROUTER_*` env variables.

## Важные функции

- `Settings`
  Основной settings model: bind host/port, startup recovery, watchdog, maintenance/runtime-convergence schedulers, dnsmasq nftset timeout, rules fetch limits, job/apply timeouts, management ports.
- `Settings.paths`
  Возвращает canonical `FWRouterPaths`; при `FWROUTER_STATE_DIR` строит test/local layout поверх этого каталога.
- `get_settings()`
  Cached singleton settings object.

## Внешние зависимости

- `/opt/fwrouter-api/.env`
- env variables с prefix `FWROUTER_`
- `core/paths.py`

## Runtime/persistent state

Конфиг читает env/.env. State не пишет.

## Boot persistence relevance

Высокая. Ошибка defaults/env здесь влияет на bind port, DB path, scheduler behavior, job timeouts и layout state directories.

## Нюансы

- В тестах часто используется `get_settings.cache_clear()` после monkeypatch env.
- `FWROUTER_STATE_DIR` меняет не только state dir, но и derived log/run dirs для isolated tests.
- `runtime_convergence_scheduler_enabled` и `runtime_convergence_interval_seconds` управляют быстрым self-heal слоем dnsmasq/dataplane отдельно от watchdog.
- `dnsmasq_nftset_timeout_seconds` задает TTL для DNS-runtime nft sets (`dns_vpn_ipv4`/`dns_direct_ipv4`), чтобы DNS materialization не раздувала live routing table бесконечно.
