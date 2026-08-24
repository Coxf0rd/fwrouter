# `/opt/fwrouter-api/fwrouter_api/services/mihomo_config_paths.py`

## Назначение

Низкоуровневые constants, resolved paths и bounded YAML metadata helpers для Mihomo config builder.

## Важные функции

- `subject_selector_name(subject_id)`
  Строит стабильное имя selector-а `fwrouter-subject-*` для subject server override.
- `_resolved_candidate_config_path()` / `_resolved_base_config_path()` / `_resolved_applied_manifest_path()`
  Возвращают live paths или test override paths через `FWROUTER_STATE_DIR` / `STATE_DIR`.
- `_safe_load_yaml(path)`
  Безопасно читает YAML mapping.
- `_count_top_level_yaml_sequence(path, key)`
  Быстро считает элементы top-level YAML sequence без полного parse больших rulesets.
- `_scan_fwrouter_config_metadata(path)`
  Читает только bounded `fwrouter:` metadata из active config для cheap runtime verifier.
- `_resolve_proxy_bypass_mark_value()`
  Берет bypass mark из applied manifest, fallback `512`.

## Нюансы

- Модуль не должен импортировать `mihomo_config.py`, чтобы не создавать cycle.
- Constants остаются re-exported через `mihomo_config.py` для обратной совместимости.
- Path override behavior важен для unit tests и не должен читать production state при `FWROUTER_STATE_DIR`.
