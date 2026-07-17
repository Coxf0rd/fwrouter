# `/opt/fwrouter-api/scripts/install-host-dependencies.sh`

## Назначение

Устанавливает host-level зависимости FWRouter на Debian/Ubuntu-like сервере через `apt-get`.

## Важные функции

- поддерживает `--dry-run` для вывода package list без установки
- поддерживает `--yes` для non-interactive `apt-get install -y`
- ставит базовые пакеты: `python3`, `python3-venv`, `python3-pip`, `curl`, `jq`, `nftables`, `iproute2`, `iptables`, `sqlite3`, `conntrack`, `dnsutils`, `ca-certificates`, `tar`, `kmod`, `procps`
- ставит Docker package candidates из доступных apt repos: `docker.io` и `docker-compose-plugin` или fallback `docker-compose`
- проверяет наличие `docker compose`
- пытается подготовить `/dev/net/tun`, если доступен `modprobe`

## Внешние зависимости

- root
- `apt-get`
- `apt-cache`
- network access к apt repositories

## Runtime/persistent state

Меняет host package state. Runtime FWRouter state не пишет.

## Boot persistence relevance

Высокая для нового сервера: без этих пакетов systemd units/preflight/apply paths могут не стартовать.

## Нюансы

- Для non-apt дистрибутивов script намеренно падает с понятным сообщением; пакетный mapping нужно добавить отдельно.
- Docker install зависит от того, какие packages есть в configured apt repositories.
