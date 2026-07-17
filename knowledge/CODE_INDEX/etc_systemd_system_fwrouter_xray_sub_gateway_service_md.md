# `/etc/systemd/system/fwrouter-xray-sub-gateway.service`

## Назначение

Follower-service для subscription gateway поверх API.

## `[Unit]`

- `After=network-online.target fwrouter-api.service docker.service`
- `Requires=fwrouter-api.service`

## `[Service]`

- `Type=simple`
- `ExecStartPre=fwrouter-wait-port.sh 127.0.0.1 5000 60 fwrouter-api`
- `ExecStart=/usr/bin/python3 /usr/local/libexec/fwrouter/fwrouter-xray-sub-gateway.py`
- `Restart=always`

## Риски

- gateway следует за API, но не знает о readiness Xray
- bind адрес `172.18.0.1` предполагает конкретную Docker network topology

## Boot persistence relevance

Средняя.
