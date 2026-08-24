# `/opt/fwrouter-api/fwrouter_api/services/mihomo_config_validation.py`

## Назначение

Structural validation helpers для generated Mihomo candidate config.

## Важные функции

- `_candidate_runtime_proxies(candidate_config)`
  Индексирует candidate `proxies` по имени.
- `_candidate_group_names(candidate_config)`
  Возвращает имена candidate `proxy-groups`.
- `_validate_candidate_with_binary(candidate_path)`
  Best-effort `mihomo`/`clash-meta`/`clash -t -f` validation, если binary доступен.
- `_validate_candidate_structure(candidate_config, routing=None)`
  Проверяет FWRouter contract: `allow-lan`, `routing-mark`, отсутствие legacy inbounds, `vpn-auto`/`vpn-global`, mixed listener, transparent REDIR/TPROXY listeners, sub-rules fallback и Xray handoff targets.

## Runtime/persistent state

- persistent state не пишет
- читает contours и bypass mark helpers
- вызывается фасадом `mihomo_config.validate_mihomo_candidate_config(...)`

## Нюансы

- Модуль не должен становиться публичным API; публичный compatibility path остается `mihomo_config.py`.
- `_validate_candidate_structure(...)` лениво импортирует facade callbacks для fallback/runtime proxy count, чтобы старые monkeypatch hooks продолжали работать.
- Ошибки validation должны оставаться стабильными: UI/logs и tests завязаны на `MIHOMO_*` error_code.
