# Xray

## Role

Xray is a separate runtime for client subscriptions and related subject-binding logic. It is not the owner of the host TProxy dataplane. Xray client traffic is forced to VPN egress through explicit handoff/binding paths.

## Main Files

- `/opt/fwrouter-xray/docker-compose.yml`
- `/var/lib/fwrouter-v2/xray/config.json`
- `fwrouter_api/services/xray.py`
- `fwrouter_api/adapters/xray.py`
- `fwrouter_api/services/xray_subscription.py`
- `fwrouter_api/services/xray_handoff.py`
- `/usr/local/libexec/fwrouter/fwrouter-xray-sub-gateway.py`
- `/etc/systemd/system/fwrouter-xray.service`
- `/etc/systemd/system/fwrouter-xray-sub-gateway.service`

## Runtime Contract

- container: `fwrouter-xray`
- Docker network: `fwrouter_proxy` by default; set `FWROUTER_DOCKER_PROXY_NETWORK=proxy_net` for legacy deployments
- logs: `/var/log/fwrouter/xray`
- subscription gateway: `172.18.0.1:5055`
- generated config: `/var/lib/fwrouter-v2/xray/config.json`

Per-client traffic accounting uses Xray `StatsService` keys such as `user>>>email>>>traffic>>>downlink/uplink`. Attribution falls back to `xray:<client_uuid>` if the runtime binding is temporarily missing.

Runtime binding materialization must be idempotent. If the resulting `config.json` does not change, backend must not restart `fwrouter-xray`; polling and accounting should not create short client disconnects.

## Public Subscription Profiles

`fwrouter_api/services/subscription_profiles.py` builds Clash/Mihomo, raw/base64 VLESS, and Happ payloads based on query, app, and user agent.

`fwrouter_api/services/xray_subscription.py` builds canonical VLESS URIs. `fwrouter_api/services/xray_handoff.py` assigns managed egress tags/listeners for Xray handoff into Mihomo; this is an explicit path, not normal LAN transparent ingress.

## UI Read Model

Public subscription profile nodes may create multiple real `explicit_external_client` subject rows for one logical client. UI/read-model aggregates them into synthetic `xray-subscription:<client-label>` subjects. Runtime/accounting detail rows such as `sub-*` and service clients are hidden from normal user lists when they would create duplicate/noisy rows.

## Boot Relevance

- The configured Docker network must exist before service start.
- Generated config must exist in persistent state.
- API must be ready before the subscription gateway starts.

## Risks

- The Xray unit does not create the Docker network itself. The installer creates it only when a managed runtime component is selected.
- Gateway depends on API readiness rather than direct Xray readiness.
- `latest` images increase nondeterministic runtime behavior risk.
- Non-idempotent config writes can restart clients during accounting/polling.
