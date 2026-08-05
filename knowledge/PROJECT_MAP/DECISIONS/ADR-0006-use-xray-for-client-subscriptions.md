# 0006: Use Xray For Client Subscriptions And Bindings

## Status

Accepted.

## Context

FWRouter stores Xray clients, subscriptions, and runtime bindings separately from the main transparent dataplane.

## Decision

Use Xray as a separate runtime for client subscriptions and related binding artifacts.

## Consequences

- Transparent egress and client subscription planes remain separated.
- Xray clients can be forced to VPN through explicit handoff paths.
- The optional managed Xray install gains another runtime dependency and Docker network contract.
- The configured Docker network is external to the unit file. Installer-created managed runtime installs default to `fwrouter_proxy`; legacy hosts can set `FWROUTER_DOCKER_PROXY_NETWORK=proxy_net`.

## Related Files

- `/opt/fwrouter-xray/docker-compose.yml`
- `/opt/fwrouter-api/fwrouter_api/services/xray.py`
- `/opt/fwrouter-api/fwrouter_api/adapters/xray.py`
- `/usr/local/libexec/fwrouter/fwrouter-xray-sub-gateway.py`
