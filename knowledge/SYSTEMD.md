# Systemd

## Основные units

### `fwrouter-mihomo.service`

- Тип: `oneshot`, `RemainAfterExit=yes`
- `After/Wants=network-online.target docker.service`
- `ExecStart`: preflight, `docker compose up -d mihomo`, wait `127.0.0.1:5200`
- `ExecStop`: `docker compose stop mihomo`
- Риск: сам unit не перезапускается автоматически, устойчивость обеспечивается контейнером `restart: unless-stopped`

### `fwrouter-xray.service`

- Тип: `oneshot`, `RemainAfterExit=yes`
- `After/Wants=network-online.target docker.service`
- `ExecStartPre=/usr/bin/docker network inspect proxy_net`
- `ExecStart`: preflight, `docker compose up -d fwrouter-xray`
- Риск: внешний Docker network `proxy_net` должен существовать до boot

### `fwrouter-api.service`

- Тип: `simple`
- `After/Wants=network-online.target docker.service dnsmasq.service fwrouter-mihomo.service fwrouter-xray.service`
- `ExecStartPre=fwrouter-boot-preflight.sh`
- `ExecStart=... uvicorn fwrouter_api.main:app`
- `Restart=on-failure`
- `RuntimeDirectory=fwrouter-v2`
- Риск: backend зависит от dockerized runtimes и `dnsmasq`

### `fwrouter-xray-sub-gateway.service`

- Тип: `simple`
- `After=network-online.target fwrouter-api.service docker.service`
- `Requires=fwrouter-api.service`
- `ExecStartPre=wait-port 127.0.0.1 5000`
- `Restart=always`
- Риск: если API flap'ает, gateway будет циклически перезапускаться, что приемлемо как follower-service

### `fwrouter-subscription-refresh.service`

- Тип: `oneshot`
- `After=network-online.target fwrouter-api.service docker.service`
- `Requires=fwrouter-api.service`
- `ExecStart=/usr/local/sbin/fwrouter-subscription-refresh-job`
- Риск: создает backend job и зависит от API, а не читает subscription state напрямую.
- Wrapper poll'ит job до финального статуса, потому что `subscription_refresh_prepare` может идти дольше короткого `run_now` ожидания backend API.

### `fwrouter-traffic-collect.service`

- Тип: `oneshot`
- `After/Wants=fwrouter-api.service`
- `ExecStart=/usr/local/libexec/fwrouter/traffic-collect-api.sh`
- Риск: `JOB_CONFLICT` от уже идущего collect считается harmless skip; шум в journald здесь обычно важнее, чем немедленный повтор.

### `fwrouter-maintenance.service`

- Тип: `oneshot`
- `ExecStart=/opt/fwrouter-api/.venv/bin/python -m fwrouter_api_maintenance`
- Риск: запускает real maintenance, не dry-run; cleanup должен оставаться conservative.

### `fwrouter-jobs-retention-dry-run.service`

- Тип: `oneshot`
- `After=network-online.target fwrouter-api.service`
- `Requires=fwrouter-api.service`
- `ExecStart=/usr/local/sbin/fwrouter-jobs-retention-dry-run`
- Риск: diagnostic/dry-run unit; не должен удалять jobs/artifacts.

## Timers

- `fwrouter-subscription-refresh.timer`
  `OnCalendar=03:00 и 15:00`, `Persistent=true`
- `fwrouter-maintenance.timer`
  `OnCalendar=04:45`, `Persistent=true`, `RandomizedDelaySec=10m`
- `fwrouter-traffic-collect.timer`
  каждые 3 минуты после `OnBootSec=2min`
  этот timer нужен не только для статистики: без свежих `traffic_counter_snapshots` watchdog suppress'ит `vpn-auto` auto-failover
  wrapper `/usr/local/libexec/fwrouter/traffic-collect-api.sh` в штатном success-path молчит; `JOB_CONFLICT` от already-running collect считается harmless skip, чтобы timer не шумел в journald
- `fwrouter-jobs-retention-dry-run.timer`
  `OnCalendar=04:30`, `Persistent=true`, `RandomizedDelaySec=10m`; запускает только dry-run диагностику retention.

## Boot ordering summary

- network + docker
- Mihomo/Xray
- API
- gateway
- timers

## Race condition controls

- preflight перед основными сервисами
- wait-port для readiness checks
- явные `After/Wants`
- startup recovery в backend на случай, если kernel dataplane отсутствует после reboot

## Что проверять при изменениях unit-файлов

- `After/Wants/Requires`
- `Restart=` и `RestartSec=`
- `ExecStartPre`
- root/capabilities
- зависимость от `docker.service`, `dnsmasq.service`, `network-online.target`
- что unit остается идемпотентным при `daemon-reload && restart`
