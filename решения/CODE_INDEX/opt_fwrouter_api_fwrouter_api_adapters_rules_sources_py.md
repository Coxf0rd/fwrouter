# `/opt/fwrouter-api/fwrouter_api/adapters/rules_sources.py`

## Назначение

HTTP/Git adapter для загрузки больших DIRECT/VPN rule lists.

## Важные функции

- `RulesSourceAdapter.fetch_big_direct_sources()`
- `RulesSourceAdapter.fetch_big_vpn_sources()`
- `_parse_git_source(...)`
- `_fetch_git_source(...)`
- `_normalize_values(...)`
- `RulesSourceFetchError`

## Внешние зависимости

- `core/config.py` для URL, timeout, user-agent и max bytes
- `urllib.request`
- `git` через subprocess для git-backed source specs

## Runtime/persistent state

Сам adapter state не пишет. Результаты передаются в rules pipeline, где уже валидируются, записываются и promote-ятся.

## Boot persistence relevance

Средняя. Ошибка в этом слое не ломает boot напрямую, но ломает `/rules/full-update` и обновление selective/direct rule artifacts.

## Нюансы

- Git source и HTTP source должны сходиться к одному payload contract: values, source URLs, version metadata.
- Ограничения `rules_fetch_timeout_seconds` и `rules_fetch_max_bytes` защищают backend от зависания/перерасхода памяти на внешних списках.

