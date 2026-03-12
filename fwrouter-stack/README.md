# fwrouter-stack (public export)

Санитизированная (без секретов) часть стэка для домашнего шлюза:

- `fwrouter/` — локальная UI/API панель (FastAPI) + docker-compose
- `fwrouter/docker-compose.mihomo.yml` — основной compose для Mihomo (host network + TUN)
- `fwrouter/docker-compose.mihomo2.yml` — вариант для второй копии Mihomo (legacy/миграции)
- `host-sbin/` — скрипты для `/usr/local/sbin/fwrouter-*`
- `host-systemd/` — systemd units для `/etc/systemd/system/*`
- `host-etc-fwrouter/` — примеры для `/etc/fwrouter/*` (без реальных доменов/секретов)

## Приватность

В репо специально НЕ хранятся:

- URL подписок, HWID, REALITY private key, TLS ключи/сертификаты
- сгенерённые подписки (`sub-vpn*`), сгенерённые конфиги Xray

## Runtime (где что живёт на хосте)

- Конфиги: `/etc/fwrouter/*`
- Состояние: `/var/lib/fwrouter/*`
- Скрипты: `/usr/local/sbin/fwrouter-*`
- Юниты: `/etc/systemd/system/fwrouter-*`
