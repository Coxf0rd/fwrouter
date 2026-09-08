# `/opt/fwrouter-api/fwrouter_api/services/subscription.py`

## Назначение

Управляет subscription URL state, validation и inventory refresh в SQLite.

## Важные функции

- `validate_subscription_url(url)`
- `normalize_subscription_urls(urls)`
- `get_subscription_state()`
- `subscription_registry_import_plan(state=...)`
- `save_subscription_url(url, metadata=...)`
- `refresh_subscription_inventory_batch(urls, metadata=...)`
- inventory refresh/upsert helpers для серверов
  При upsert сохраняет `country_code`, полученный parser adapter. Если parser распознал emoji-флаг, текущие и будущие subscription-серверы получают ISO-like код страны для UI flags.

## Внешние зависимости

- DB
- URL parsing
- provider adapter/import path

## Runtime/persistent state

- пишет `subscription_state`
- обновляет server inventory из subscription refresh
- `subscription_state.url` остается legacy/fallback URL, а authoritative multi-subscription registry хранится без отдельной таблицы в `subscription_state.metadata_json.subscriptions.items`
- write/refresh paths нормализуют legacy-only state в backend registry; read paths не создают registry автоматически
- для каждого сохраненного subscription source metadata хранит enabled/status/timestamps и last-good server snapshot; fetch/parse failure одного source не считается доказательством, что его серверы исчезли
- batch refresh и periodic/manual refresh сначала скачивают/парсят все active persistent subscription URL, затем один раз upsert-ят effective union серверов; это предотвращает ложный `missing` для серверов из другого source
- `servers.country_code` является read-model metadata для UI/server list; dataplane не должен зависеть от наличия кода

## Boot persistence relevance

Средняя. Subscription state переживает reboot и влияет на inventory/config regeneration.
