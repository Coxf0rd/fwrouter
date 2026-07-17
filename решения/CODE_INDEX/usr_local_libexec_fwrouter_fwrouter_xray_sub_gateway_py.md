# `/usr/local/libexec/fwrouter/fwrouter-xray-sub-gateway.py`

## Назначение

Поднимает HTTP gateway, который проксирует subscription requests в `fwrouter-api`.

## Важные классы

- `ThreadingHTTPServer` используется как runtime server
- request handler ограничивает допустимые пути

## Важные функции

- `main()`
  Стартует bind на `172.18.0.1:5055`.

## Внешние зависимости

- API upstream `127.0.0.1:5000`
- Python stdlib HTTP stack

## Runtime/persistent state

- не хранит persistent state

## Boot persistence relevance

Средняя. Нужен для subscription delivery path.

## Нюансы

- разрешены только узкие subscription URL paths
- unit ждет readiness API через `wait-port`
