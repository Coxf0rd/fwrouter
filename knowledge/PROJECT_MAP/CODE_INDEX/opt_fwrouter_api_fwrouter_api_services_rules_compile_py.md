# `/opt/fwrouter-api/fwrouter_api/services/rules_compile.py`

## Назначение

Validation/normalization/compiler layer для FWRouter rules.

## Важные функции

- `_configured_rules_sources()`
  Читает configured `big_direct` / `big_vpn` source URLs и fetch limits.
- `_validate_big_vpn_source_policy(info)`
  Проверяет, что `big_vpn` source использует допустимую path policy.
- `validate_manual_rules(text)`
  Парсит manual rules формата `ACTION VALUE`, нормализует action aliases и запрещает VPN для protected local/service networks.
- `validate_value_list(text, action, source)`
  Валидирует large-list rules, нормализует domains в suffix entries для big lists, collapse CIDR и пропускает protected VPN entries для external big lists.
- `build_effective_rules_artifact(...)`
  Собирает итоговый artifact с priority order: protected -> manual -> static_direct -> big_direct -> big_vpn -> selective_default.
- `render_effective_rules_text(effective_artifact)`
  Рендерит human-readable effective rules text.

## Runtime/persistent state

- persistent state не пишет
- возвращает DTO/artifacts для `rules_state.py`, `rules_artifacts.py`, `rules_jobs.py`

## Нюансы

- Модуль не должен импортировать `rules.py`, чтобы не создавать cycle.
- Все публично используемые constants/functions re-export-ятся через `rules.py` для совместимости.
- Protected local/service networks всегда имеют DIRECT приоритет выше external VPN lists.
