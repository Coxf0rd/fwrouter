# `/opt/fwrouter-api/scripts/post-code-reconcile.sh`

## Назначение

Helper после code update: daemon-reload, restart API, ожидание health, затем post-clean-reset bootstrap и status dumps.

## Важные функции

- `systemctl daemon-reload`
- restart configurable service, default `fwrouter-api.service`
- wait `/health`
- запускает `post-clean-reset-bootstrap.sh`
- печатает subscription/runtime JSON

## Внешние зависимости

- `systemctl`
- `curl`
- `python3`
- backend API

## Runtime/persistent state

Перезапускает backend и может вызвать sync через bootstrap helper.

## Boot persistence relevance

Средняя. Удобен после deploy/code reconcile, но не должен заменять formal install script.

## Нюансы

- Если API не стал healthy за timeout, script падает.

