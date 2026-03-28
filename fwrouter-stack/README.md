# fwrouter-stack

Санитизированный экспорт базового стека домашнего шлюза.

Внутри:

- `fwrouter/` — FastAPI UI/API, статические файлы, Dockerfile и compose
- `host-sbin/` — скрипты для `/usr/local/sbin/fwrouter-*`
- `host-systemd/` — systemd-юниты для `/etc/systemd/system/*`
- `host-etc-fwrouter/` — публичные примеры `/etc/fwrouter/*`
- `ansible/` — установка и раскладка на целевой хост

## Что входит в функциональность

- пользовательская и админская панели
- выбор VPN-сервера с живым пингом
- `vpn-auto` с кандидатами и скрытием серверов для пользователя
- `DIRECT`, `VPN`, `SELECTIVE`
- отдельный режим для трафика самого шлюза
- Re-filter sync из релизов GitHub
- обновление `rules.d` через UI
- локальные SVG-флаги стран в UI
- client-side проверка IP для `DIRECT` и `VPN`

## Где что живет на хосте

- `/app/fwrouter` — compose и код API
- `/etc/fwrouter` — конфиги, правила, `mihomo2/config.yaml`
- `/usr/local/sbin/fwrouter-*` — прикладные скрипты
- `/etc/systemd/system/fwrouter-*` — systemd-юниты
- `/var/lib/fwrouter` — runtime state, кэш, логи, служебные файлы

## Важные замечания

- `mihomo2` в этой схеме один, поэтому выбранный в `fwrouter` upstream-сервер общий для трафика, который идет через шлюзовый `mihomo`
- per-device routing управляет тем, идет ли клиент через VPN, но не дает каждому клиенту свой отдельный upstream внутри одного `mihomo`
- проверки IP в UI выполняются из браузера клиента; это важно для корректного отображения клиентского `DIRECT`/`VPN` IP

## Что санитизировано

- секреты и реальные токены удалены
- публичные конфиги оставлены как примеры
- VLESS/TLS/REALITY чувствительные данные сюда не включаются

## Связанные файлы

- [README.md](/app/_export/fwrouter-github/README.md)
- [fwrouter/VPN_RECOVERY.md](/app/_export/fwrouter-github/fwrouter-stack/fwrouter/VPN_RECOVERY.md)
- [ansible/README.md](/app/_export/fwrouter-github/fwrouter-stack/ansible/README.md)
