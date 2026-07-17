# `/opt/fwrouter-api/fwrouter_api/services/subscription.py`

## Назначение

Управляет subscription URL state, validation и inventory refresh в SQLite.

## Важные функции

- `validate_subscription_url(url)`
- `get_subscription_state()`
- `save_subscription_url(url, metadata=...)`
- inventory refresh/upsert helpers для серверов
  При upsert сохраняет `country_code`, полученный parser adapter. Если parser распознал emoji-флаг, текущие и будущие subscription-серверы получают ISO-like код страны для UI flags.

## Внешние зависимости

- DB
- URL parsing
- provider adapter/import path

## Runtime/persistent state

- пишет `subscription_state`
- обновляет server inventory из subscription refresh
- `servers.country_code` является read-model metadata для UI/server list; dataplane не должен зависеть от наличия кода

## Boot persistence relevance

Средняя. Subscription state переживает reboot и влияет на inventory/config regeneration.
