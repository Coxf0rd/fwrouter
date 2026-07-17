# `/etc/systemd/system/fwrouter-xray.service`

## Назначение

Поднимает Xray container.

## `[Unit]`

- `After/Wants=network-online.target docker.service`

## `[Service]`

- `Type=oneshot`
- `RemainAfterExit=yes`
- `ExecStartPre=/usr/bin/docker network inspect proxy_net`
- `ExecStart=fwrouter-boot-preflight.sh`
- `ExecStart=docker compose ... up -d fwrouter-xray`
- `ExecStop=docker compose ... stop fwrouter-xray`

## Риски

- старт зависит от внешней Docker network `proxy_net`
- unit не ждет конкретный xray port/readiness

## Boot persistence relevance

Высокая.
