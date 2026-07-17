# Компоненты

## Control plane

- `/opt/fwrouter-api/fwrouter_api/main.py`
  Точка входа FastAPI.
- `/opt/fwrouter-api/fwrouter_api/services/bootstrap.py`
  Startup recovery после reboot и после backend restart.
- `/opt/fwrouter-api/fwrouter_api/services/apply_orchestrator.py`
  Координация apply mutations и drift-repair.
- `/opt/fwrouter-api/fwrouter_api/services/servers.py`
  Глобальный routing intent и связь с Mihomo selectors.
- `/opt/fwrouter-api/fwrouter_api/services/xray.py`
  Runtime bindings, client subscriptions, sync subject mappings.

## Dataplane helpers

- `/usr/local/libexec/fwrouter/dataplane-apply.sh`
  Применяет `nftables` + `ip rule` + `ip route`.
- `/usr/local/libexec/fwrouter/dataplane-check.sh`
  Проверяет candidate/live dataplane contract.
- `/usr/local/libexec/fwrouter/dataplane-rollback.sh`
  Откатывает live table и routing contract.

## Runtime containers

- `/opt/fwrouter-mihomo/docker-compose.yml`
  Host-network контейнер Mihomo.
- `/opt/fwrouter-xray/docker-compose.yml`
  Xray в external Docker network `proxy_net`.

## Boot/service layer

- `/etc/systemd/system/fwrouter-mihomo.service`
- `/etc/systemd/system/fwrouter-xray.service`
- `/etc/systemd/system/fwrouter-api.service`
- `/etc/systemd/system/fwrouter-xray-sub-gateway.service`
- timers на maintenance, traffic collect, subscription refresh, retention dry-run

## Diagnostics and installation

- `/opt/fwrouter-api/scripts/install-server-tree.sh`
- `/opt/fwrouter-api/scripts/bootstrap-state.sh`
- `/opt/fwrouter-api/scripts/check_boot_persistence.sh`
- `/usr/local/libexec/fwrouter/traffic-collect.sh`
