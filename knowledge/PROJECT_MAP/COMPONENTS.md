# Components

## Control Plane

- `/opt/fwrouter-api/fwrouter_api/main.py`
  FastAPI entry point.
- `/opt/fwrouter-api/fwrouter_api/services/bootstrap.py`
  Startup recovery after reboot and backend restart.
- `/opt/fwrouter-api/fwrouter_api/services/apply_orchestrator.py`
  Apply mutation coordination and drift repair.
- `/opt/fwrouter-api/fwrouter_api/services/servers.py`
  Global routing intent and Mihomo selector integration.
- `/opt/fwrouter-api/fwrouter_api/services/xray.py`
  Runtime bindings, client subscriptions, and subject mapping sync.

## Dataplane Helpers

- `/usr/local/libexec/fwrouter/dataplane-apply.sh`
  Applies `nftables` + `ip rule` + `ip route`.
- `/usr/local/libexec/fwrouter/dataplane-check.sh`
  Validates candidate/live dataplane contract.
- `/usr/local/libexec/fwrouter/dataplane-rollback.sh`
  Rolls back the live table and routing contract.

## Runtime Containers

- `/opt/fwrouter-mihomo/docker-compose.yml`
  Optional managed Mihomo container. In the module model this is represented by `vpn` with `lifecycle_mode=managed`.
- `/opt/fwrouter-xray/docker-compose.yml`
  Optional managed Xray container in the configured external Docker network, default `fwrouter_proxy`.

External integrations use `lifecycle_mode=external`: FWRouter may probe or consume the runtime, but must not create units, containers, networks, or run lifecycle actions for it.
This is a generic ownership marker, not a product list. Externally managed
ingress or egress services connect through registry contracts and manual runtime
setup outside FWRouter.

## Boot / Service Layer

- `/etc/systemd/system/fwrouter-mihomo.service`
- `/etc/systemd/system/fwrouter-xray.service`
- `/etc/systemd/system/fwrouter-api.service`
- `/etc/systemd/system/fwrouter-xray-sub-gateway.service`
- timers for maintenance, traffic collection, subscription refresh, and retention dry-run

## Diagnostics And Installation

- `/opt/fwrouter-api/scripts/install-server-tree.sh`
- `/opt/fwrouter-api/scripts/bootstrap-state.sh`
- `/opt/fwrouter-api/scripts/check_boot_persistence.sh`
- `/usr/local/libexec/fwrouter/traffic-collect.sh`
