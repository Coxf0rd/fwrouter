# External Management Contract

FWRouter API допускает внешние клиенты управления: локальная automation, voice bridge, bot, dashboard, CLI-wrapper. Backend не знает про конкретную интеграцию и принимает только универсальную attribution-модель.

## Базовые поля

Мутирующие запросы могут передавать:

- `requested_by`
  Opaque строка-источник. Рекомендуемый формат для внешних клиентов: `external_client:<client_name>`.
- `management_context`
  Объект с уточняющими полями:
  - `source_type`: тип источника, обычно `external_client`
  - `client_name`: имя клиента управления
  - `channel`: канал вызова, например `local_api`, `voice`, `bot`, `webhook`
  - `actor`: пользователь/субъект, если известен
  - `action`: конкретное действие клиента
  - `request_id`: correlation id, если клиент умеет его выдавать

Для `external_client` обязательны `client_name` и `action`. Если их не хватает, endpoint возвращает ошибку до выполнения действия.

## Ошибка неполного контекста

```json
{
  "ok": false,
  "data": {
    "management_attribution": {
      "requested_by": "external_client",
      "source_type": null,
      "client_name": null,
      "channel": null,
      "actor": null,
      "action": null,
      "request_id": null,
      "attribution_complete": false,
      "attribution_missing": ["client_name", "action"]
    }
  },
  "error": {
    "code": "MANAGEMENT_ATTRIBUTION_INCOMPLETE",
    "message": "External management request is missing required attribution fields.",
    "missing_fields": ["client_name", "action"]
  }
}
```

## Пример: выбрать лучший `vpn-auto` сервер

```bash
curl -sS -X POST http://127.0.0.1:5500/api/v2/selector/vpn-auto/switch \
  -H 'Content-Type: application/json' \
  -d '{
    "confirm_switch": true,
    "requested_by": "external_client:my-automation",
    "management_context": {
      "source_type": "external_client",
      "client_name": "my-automation",
      "channel": "local_api",
      "action": "switch_best_vpn_auto_server",
      "actor": "operator"
    }
  }'
```

## Пример: выбрать global fixed server

```bash
curl -sS -X POST http://127.0.0.1:5500/api/v2/routing/global/fixed-server \
  -H 'Content-Type: application/json' \
  -d '{
    "server_id": "server-id-or-name-from-inventory",
    "confirm_switch": true,
    "requested_by": "external_client:my-automation",
    "management_context": {
      "source_type": "external_client",
      "client_name": "my-automation",
      "channel": "voice",
      "action": "set_global_fixed_server",
      "actor": "operator"
    }
  }'
```

Global fixed server имеет backend TTL 24 часа; TTL хранится в FWRouter state, не у внешнего клиента.

## Пример: сбросить global fixed server в auto

`DELETE` endpoint принимает context через query params:

```bash
curl -sS -X DELETE \
  'http://127.0.0.1:5500/api/v2/routing/global/fixed-server?confirm_switch=true&requested_by=external_client:my-automation&management_client_name=my-automation&management_channel=local_api&management_action=clear_global_fixed_server&management_actor=operator'
```

## Пример: сменить global mode

```bash
curl -sS -X POST http://127.0.0.1:5500/api/v2/routing/global \
  -H 'Content-Type: application/json' \
  -d '{
    "mode": "selective",
    "requested_by": "external_client:my-automation",
    "management_context": {
      "source_type": "external_client",
      "client_name": "my-automation",
      "channel": "bot",
      "action": "set_global_mode:selective",
      "actor": "operator"
    }
  }'
```

## Logging

Успешные external management действия пишутся в operational logs с:

- `requested_by`
- normalized `management_attribution`
- выбранным сервером или режимом
- `active_before` / `active_after`, где применимо
- ping details, где применимо

UI отображает такие события в обычном журнале операторских действий.
