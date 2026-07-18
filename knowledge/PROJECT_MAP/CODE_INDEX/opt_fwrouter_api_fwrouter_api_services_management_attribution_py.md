# `/opt/fwrouter-api/fwrouter_api/services/management_attribution.py`

## Назначение

Нормализует source attribution для внешних клиентов управления без привязки к конкретной интеграции.

## Важные функции

- `build_management_attribution(...)`
  Собирает `requested_by`, `source_type`, `client_name`, `channel`, `actor`, `action`, `request_id` и флаги полноты.

- `build_incomplete_attribution_error(...)`
  Возвращает API-ready ошибку `MANAGEMENT_ATTRIBUTION_INCOMPLETE`, если external client не передал обязательные поля.

## Нюансы

- `requested_by` остается opaque строкой для совместимости.
- Для `external_client` обязательны `client_name` и `action`; source/client могут быть выведены из `requested_by` формата `external_client:<name>`.
- Backend не должен знать про конкретные локальные интеграции и не должен ветвиться по их именам.
