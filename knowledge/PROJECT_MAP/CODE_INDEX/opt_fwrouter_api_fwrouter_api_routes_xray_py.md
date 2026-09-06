# `/opt/fwrouter-api/fwrouter_api/routes/xray.py`

## Назначение

API для Xray status, clients CRUD, reload, subject sync и subscription export.

## Важные endpoints

- `GET /api/v2/xray`
- `GET/POST/PATCH/DELETE /api/v2/xray/clients...`
- `DELETE /api/v2/xray/subscription-profiles/{token}`
- `POST /api/v2/xray/reload`
- `POST /api/v2/xray/sync-subjects`
- `GET /api/v2/xray/clients/{client_id}/subscription`
- `GET /api/v2/xray/clients/{client_id}/subscription.txt`
- `GET /s/{token}`

## Внешние зависимости

- `services/xray.py`
- request-based format negotiation (`clash` vs `vless`)
- background reconcile path for public subscription profiles

## Runtime/persistent state

- может менять Xray clients state и вызывать runtime reload
- routes остаются тонкими; service-layer mutations (`clients` create/update/delete, subject sync, `reload`, binding materialization, public profile reconcile) требуют `xray.lifecycle_mode=managed`

## Boot persistence relevance

Средняя/высокая. Важен для client subscription plane после reboot.

## Нюансы

- публичные subscription responses строятся с учетом User-Agent/Accept; VLESS public host берется из `X-Forwarded-Host`/`Host`, env-настройки Xray public endpoint используются как fallback
- часть endpoints использует service-call wrapper с унифицированной error surface
- post-response reconcile для `GET /s/{token}` должен идти в отдельном daemon worker, а не как FastAPI background task, иначе `fwrouter-api` может зависать на graceful shutdown
- Settings UI показывает создание как domain action `External client`; `POST /xray/clients` остается compatibility/write-adapter path, создает `/s/{name}` subscription profile для link suffix и не должен возвращать Xray/VLESS как верхний уровень UI-модели.
