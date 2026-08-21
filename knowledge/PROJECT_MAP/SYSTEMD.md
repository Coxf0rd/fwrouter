# Systemd

## Main Units

### `fwrouter-mihomo.service`

- type: `oneshot`, `RemainAfterExit=yes`
- ordering: `After/Wants=network-online.target docker.service`
- start: preflight, `docker compose up -d mihomo`, wait for `127.0.0.1:5200`
- stop: `docker compose stop mihomo`
- risk: the unit itself does not restart automatically; container resilience comes from Docker `restart: unless-stopped`

### `fwrouter-xray.service`

- type: `oneshot`, `RemainAfterExit=yes`
- ordering: `After/Wants=network-online.target docker.service`
- preflight: requires TUN via `FWROUTER_REQUIRE_TUN=1`; `docker network inspect "$FWROUTER_DOCKER_PROXY_NETWORK"`
- start: preflight, `docker compose up -d fwrouter-xray`
- risk: configured Docker network must exist before boot; default is `fwrouter_proxy`, legacy deployments can set `FWROUTER_DOCKER_PROXY_NETWORK=proxy_net`

### `fwrouter-api.service`

- type: `simple`
- ordering: `After/Wants=network-online.target`
- preflight: `fwrouter-boot-preflight.sh` for core host readiness only; it does not require Docker, Mihomo, Xray, dnsmasq, or TUN unless a runtime unit sets `FWROUTER_REQUIRE_TUN=1`
- start: backend `uvicorn fwrouter_api.main:app`
- restart: `on-failure`
- runtime dir: `fwrouter-v2`
- risk: backend can start as core control plane before optional runtimes; features that need a missing integration report degraded runtime status

### `fwrouter-xray-sub-gateway.service`

- type: `simple`
- ordering: `After=network-online.target fwrouter-api.service docker.service`
- requires: `fwrouter-api.service`
- preflight: wait for `127.0.0.1:5000`
- restart: `always`
- risk: it is a follower service; API flaps cause gateway restarts

### `fwrouter-docker-subject-events.service`

- type: `simple`
- ordering: `After/Wants=docker.service fwrouter-api.service`
- start: `/usr/local/libexec/fwrouter/docker-subject-events.sh`
- restart: `always`
- risk: this is an inventory accelerator, not the source of truth; backend periodic scans must still work

### `dnsmasq.service` drop-in

- path: `/etc/systemd/system/dnsmasq.service.d/fwrouter-restart.conf`
- ownership: FWRouter-owned drop-in for the distribution `dnsmasq.service`
- restart: `on-failure`, `RestartSec=10s`
- risk: keeps LAN DNS/DHCP recovering after runtime nftset materialization failures without replacing the package unit

### `fwrouter-subscription-refresh.service`

- type: `oneshot`
- ordering: `After=network-online.target fwrouter-api.service docker.service`
- requires: `fwrouter-api.service`
- start: `/usr/local/sbin/fwrouter-subscription-refresh-job`
- risk: creates backend jobs through API and polls until terminal status

### `fwrouter-traffic-collect.service`

- type: `oneshot`
- ordering: `After/Wants=fwrouter-api.service`
- start: `/usr/local/libexec/fwrouter/traffic-collect-api.sh`
- risk: `JOB_CONFLICT` from an existing collect is a harmless skip

### `fwrouter-maintenance.service`

- type: `oneshot`
- start: `/opt/fwrouter-api/.venv/bin/python -m fwrouter_api_maintenance`
- risk: runs real maintenance, so cleanup must remain conservative

### `fwrouter-jobs-retention-dry-run.service`

- type: `oneshot`
- ordering: `After=network-online.target fwrouter-api.service`
- requires: `fwrouter-api.service`
- start: `/usr/local/sbin/fwrouter-jobs-retention-dry-run`
- risk: diagnostic only; must not delete jobs or artifacts

## Timers

- `fwrouter-subscription-refresh.timer`
- `fwrouter-maintenance.timer`
- `fwrouter-jobs-retention-dry-run.timer`
- `fwrouter-traffic-collect.timer`
  Runs every 60 seconds after `OnBootSec=2min` with `AccuracySec=1s`; this
  timer is part of the watchdog signal path, not just UI statistics.

## Deployment Rules

Changing unit files requires deploying the host component and running `systemctl daemon-reload`. Enable/disable semantics belong to installer operations, not ad-hoc edits in live `/etc`.
