# `/opt/fwrouter-api/fwrouter_api/adapters/mihomo.py`

## Назначение

HTTP adapter для Mihomo controller. Читает runtime health, selectors, active server и переключает selector targets без прямого редактирования generated config.

## Важные функции

- `MihomoHttpAdapter.health()`
  Главный runtime probe для control-plane. После фикса больше не грузит весь большой `config.yaml` через `yaml.safe_load` на hot path; вместо этого использует lightweight text scanning для controller secret, listener ports и transparent listener bind, а также best-effort `/connections` observation для `transparent_tcp_session_materialized` / `transparent_udp_session_materialized`.

- `_scan_listener_ports()`
  Быстрый текстовый разбор `listeners:` секции для `mixed/tproxy/redir` портов. Теперь это один из canonical источников transparent contour, потому что FWRouter снова держит transparent ingress через named listeners.

- `_scan_transparent_listeners()`
  Специализированный text parser для managed `fwrouter-redir` и `fwrouter-tproxy`. Нужен, чтобы health различал canonical split-listener contour и старый top-level fallback, включая listener `rule`/`proxy` target.

- `_config_runtime_details()`
  Собирает runtime contract: mixed/tproxy/redir ports, contour flags, managed listener bind/rule/proxy, loopback-bound status и раздельный TCP/UDP readiness contract. Managed split listeners `fwrouter-redir`/`fwrouter-tproxy` теперь каноничны; top-level legacy ports больше не должны делать contour falsely healthy.

## Внешние зависимости

- `httpx` к `http://127.0.0.1:5200`
- generated Mihomo config
- `contours.json`

## Runtime/persistent state

- не пишет persistent state
- читает generated config/runtime controller state

## Boot persistence relevance

Средняя-высокая. Неправильный health adapter не ломает dataplane напрямую, но может делать runtime/UI очень медленными или falsely healthy при broken transparent contour.

## Нюансы

- transparent bind может быть wildcard `0.0.0.0` или explicit non-loopback IPv4 router bind; `127.0.0.1` недопустим
- canonical transparent contour теперь split-listener:
  - `fwrouter-redir` -> TCP `5202`
  - `fwrouter-tproxy` -> UDP `5203`
  - оба listener-а должны вести в `rule: fwrouter-transparent`; legacy-compatible `proxy: vpn-global` допустим только как старый валидный target
- top-level `redir-port`/`tproxy-port` не должны переопределять managed split listeners и не должны сами по себе объявлять contour healthy
- explicit proxy listener может оставаться loopback-only
- дорогой full-YAML parse на каждом `health()` нельзя возвращать: это напрямую бьет по `GET /api/v2/runtime` и UI latency
