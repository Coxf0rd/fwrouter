# `/usr/local/libexec/fwrouter/fwrouter-wait-port.sh`

## Назначение

Ждет доступности TCP-порта и снижает race conditions между сервисами.

## Важные функции

- принимает `host port timeout label`
- циклически пытается открыть socket
- завершает с ошибкой по timeout

## Внешние зависимости

- Python stdlib socket

## Runtime/persistent state

- state не создает

## Boot persistence relevance

Высокая как readiness helper.
