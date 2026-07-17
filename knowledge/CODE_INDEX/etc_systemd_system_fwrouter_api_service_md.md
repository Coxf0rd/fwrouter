# `/etc/systemd/system/fwrouter-api.service`

## Назначение

Основной systemd unit backend API.

## `[Unit]`

- `After=network-online.target docker.service dnsmasq.service fwrouter-mihomo.service fwrouter-xray.service`
- `Wants=...` те же зависимости

## `[Service]`

- `Type=simple`
- `WorkingDirectory=/opt/fwrouter-api`
- `EnvironmentFile=-/opt/fwrouter-api/.env`
- `ExecStartPre=/usr/local/libexec/fwrouter/fwrouter-boot-preflight.sh`
- `ExecStart=/opt/fwrouter-api/.venv/bin/python -m uvicorn fwrouter_api.main:app ...`
- `Restart=on-failure`
- `RuntimeDirectory=fwrouter-v2`

## Риски

- backend не должен стартовать раньше Mihomo/Xray
- preflight должен оставаться быстрым и идемпотентным
- `dnsmasq.service` является внешней зависимостью и может отсутствовать в dev-средах

## Boot persistence relevance

Критическая.
