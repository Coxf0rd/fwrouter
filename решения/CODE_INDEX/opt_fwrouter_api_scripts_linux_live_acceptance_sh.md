# `/opt/fwrouter-api/scripts/linux-live-acceptance.sh`

## Назначение

Live acceptance script для проверки основных API/runtime flows на сервере.

## Важные функции

- health/system/runtime/scoped-egress checks
- core bypass enable/disable smoke
- global direct mutation
- optional transfer plan
- optional subject/server override flows через env variables

## Внешние зависимости

- `curl`
- `python3`
- backend API

## Runtime/persistent state

Не read-only: может менять global routing и core bypass state. Использовать только как acceptance/smoke на контролируемой системе.

## Boot persistence relevance

Средняя. Проверяет, что critical control-plane paths живы после deploy/reboot.

## Нюансы

- Перед запуском понимать, что script выполняет real mutations.
- Env variables задают optional LAN/Tailscale subject/server scenarios.

