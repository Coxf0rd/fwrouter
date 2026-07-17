# `/opt/fwrouter-api/fwrouter_api/adapters/subscription.py`

## Назначение

Скачивает и парсит Mihomo/Clash YAML subscription в список `SubscriptionServer`.

## Важные функции

- `HttpMihomoSubscriptionAdapter.refresh(url)`
  Делает HTTP download subscription payload и передает YAML в parser.

- `parse_mihomo_subscription_yaml(text, metadata=...)`
  Читает top-level `proxies`, дедуплицирует серверы по `name`, формирует `SubscriptionRefreshResult`.

- `_country_code_from_regional_indicator_emoji(text)`
  Универсально декодирует emoji-флаг regional indicators в ISO-like country code: `🇦🇱 -> AL`, `🇲🇰 -> MK`, `🇬🇷 -> GR`. Ручной словарь emoji остается fallback, но новые страны не должны требовать отдельного добавления, если в имени есть нормальный emoji-флаг.

## Внешние зависимости

- `httpx`
- `yaml`

## Runtime/persistent state

Не пишет состояние напрямую. Persistent `servers.country_code` обновляется на уровне subscription sync/upsert в `services/subscription.py`.

## Boot persistence relevance

Средняя. Parser влияет на server inventory, который затем используется UI, Mihomo config generation и server selection.

## Нюансы

- `server_id` для subscription proxy сейчас равен `name`; это важно для совместимости с Mihomo selector targets.
- `country_code` должен быть best-effort metadata. Отсутствие кода не должно ломать dataplane, но ломает UI flags.
