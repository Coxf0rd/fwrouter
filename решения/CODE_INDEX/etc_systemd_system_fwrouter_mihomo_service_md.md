# `/etc/systemd/system/fwrouter-mihomo.service`

## Назначение

Поднимает Mihomo container и ждет controller readiness.

## `[Unit]`

- `After/Wants=network-online.target docker.service`

## `[Service]`

- `Type=oneshot`
- `RemainAfterExit=yes`
- `ExecStart=fwrouter-boot-preflight.sh`
- `ExecStart=docker compose ... up -d mihomo`
- `ExecStart=fwrouter-wait-port.sh 127.0.0.1 5200 60 mihomo-controller`
- `ExecStop=docker compose ... stop mihomo`

## Риски

- `Restart=` отсутствует на уровне unit
- health contract завязан на локальный controller port

## Boot persistence relevance

Критическая.
