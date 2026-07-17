# `/opt/fwrouter-api/fwrouter_api/services/subject_groups.py`

## Назначение

Общие helper-ы для синтетических групп subject’ов, которые существуют только в API/UI read-model и не являются строками таблицы `subjects`.

## Важные функции

- `xray_subscription_group_from_values(...)`
  Строит group key/label для Xray public subscription profile subjects вида `sub-*`. Основной источник label — `alias/display_name` формата `Client / Client / Server`; fallback — localpart email без server suffix.

- `xray_subscription_group_from_row(row)`
  Row-friendly wrapper для SQL rows из `subjects JOIN subject_xray`.

- `resolve_xray_subscription_group_subject_ids(group_subject_id)`
  Разворачивает синтетический `xray-subscription:<client-label>` в реальные `subject_id` Xray profile nodes.

## Внешние зависимости

- SQLite `subjects`
- SQLite `subject_xray`

## Нюансы

- Эти group IDs нельзя сохранять как реальные subjects без отдельной миграции.
- Runtime Xray bindings/accounting остаются per real `subject_xray`; группировка нужна только для UX и batch mode operations.
