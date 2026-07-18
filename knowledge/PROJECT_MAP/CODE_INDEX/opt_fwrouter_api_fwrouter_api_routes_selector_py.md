# `/opt/fwrouter-api/fwrouter_api/routes/selector.py`

## Назначение

API для dry-run и controlled switch `vpn-auto` selector.

## Важные endpoints

- `GET /api/v2/selector/vpn-auto`
- `POST /api/v2/selector/vpn-auto/switch`
  Принимает `requested_by` и `management_context` для external management attribution; неполный `external_client` context отклоняется до switch.

## Внешние зависимости

- selector service

## Runtime/persistent state

- switch endpoint может менять live active auto server и Mihomo selector state

## Boot persistence relevance

Средняя. Active auto server state должен синхронизироваться с persisted routing state.
