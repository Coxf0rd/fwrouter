# `/opt/fwrouter-api/fwrouter_api/services/xray_subscription.py`

## Назначение

Формирует canonical VLESS URI для public Xray subscription profiles.

## Важные функции

- `build_xray_vless_uri(client_uuid, label)`

## Внешние зависимости

Нет runtime service dependencies.

## Runtime/persistent state

State не читает и не пишет.

## Boot persistence relevance

Средняя. Константы публичного host/path/transport должны совпадать с NPM/Xray gateway routing.

## Нюансы

- Public host сейчас `xray.minisk.ru`, path `/vless`, port `443`, transport `ws`.
- URI включает TLS/SNI/ALPN/fingerprint/packetEncoding параметры.

