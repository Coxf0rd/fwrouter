# `/opt/fwrouter-api/fwrouter_api/routes/selector.py`

## Назначение

API для dry-run и controlled switch `vpn-auto` selector.

## Важные endpoints

- `GET /api/v2/selector/vpn-auto`
- `POST /api/v2/selector/vpn-auto/switch`
  Принимает `requested_by` для audit/source attribution и прокидывает его в selector service.

## Внешние зависимости

- selector service

## Runtime/persistent state

- switch endpoint может менять live active auto server и Mihomo selector state

## Boot persistence relevance

Средняя. Active auto server state должен синхронизироваться с persisted routing state.
