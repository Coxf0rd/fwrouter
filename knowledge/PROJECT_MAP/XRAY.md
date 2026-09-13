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
- backend Docker Compose probes/reloads use `/run/fwrouter-v2/docker-cli` as Docker CLI state, so hardened `fwrouter-api.service` with `ProtectHome=yes` does not depend on `/root/.docker`

Per-client traffic accounting uses Xray `StatsService` keys such as `user>>>email>>>traffic>>>downlink/uplink`. Attribution falls back to `xray:<client_uuid>` if the runtime binding is temporarily missing.

Runtime binding materialization must be idempotent. If the resulting `config.json` does not change, backend must not restart `fwrouter-xray`; polling and accounting should not create short client disconnects.

VLESS create/delete uses the shared FWRouter jobs framework. HTTP mutation requests return an accepted job instead of treating DB writes or generated config as final success. The worker performs prepare/apply/verify work and only reports success after effective Xray runtime convergence.

The dataplane invariant for an active VLESS client is:

`VLESS client -> Xray inbound vless-ws -> client email/UUID identity -> fwrouter-egress-* SOCKS outbound -> Mihomo handoff listener -> selected VPN runtime path -> Internet`

Create convergence verifies that the effective Xray config contains the client in the `vless-ws` inbound, contains the expected `fwrouter-egress-*` SOCKS outbound pointing at the Mihomo handoff listener, and contains a user-scoped routing rule from `vless-ws` to that outbound. A stale rule for the same client/user that sends traffic to `fwrouter-api` is a convergence failure and must not be considered success.

Delete convergence verifies that the client is absent from the effective runtime and that managed egress/rule residue is removed or disabled by the current lifecycle. Repeat delete is safe and becomes a no-op when the runtime and local projection no longer contain the client.

## Public Subscription Profiles

`fwrouter_api/services/subscription_profiles.py` builds Clash/Mihomo, raw/base64 VLESS, and Happ payloads based on query, app, and user agent.

`fwrouter_api/services/xray_subscription.py` builds canonical VLESS URIs. Public host comes from public subscription request headers or `FWROUTER_XRAY_PUBLIC_HOST`; path and port come from `FWROUTER_XRAY_PUBLIC_PATH`/`FWROUTER_XRAY_PUBLIC_PORT`. The subscription gateway must forward the original public `Host`/proto to the API; internal loopback/private upstream hosts are never valid client endpoints and fall back to the configured public host.

Settings external-client create immediately creates the domain-visible `/s/{name}` subscription profile and runs the existing profile reconcile/materialization. Delete disables the subscription profile identity, removes scoped operational projections (`explicit_external_client` subjects plus subject overrides) for that external client, and then reconciles generated `sub-*` runtime clients/materialization. Audit events remain in operational/technical logs; repeat no-op deletes do not emit another `external_client.deleted` event.

Public subscription GET is read-only. It does not create DB identities, run reconcile, or materialize runtime. When the managed Xray module is enabled, the renderer exports only nodes that are currently runtime-exportable: the effective Xray config must contain the VLESS client, `fwrouterBinding`, a scoped `vless-ws` user rule to `fwrouter-egress-*`, and no stale user-specific `fwrouter-api` fallback.

Subscription refresh and startup apply/reconcile run the existing profile reconcile/materialization path so persistent subscription clients can be reconstructed from intent after server inventory changes or backend restart. This keeps persistent client identity, generated Xray config, effective runtime, and public `/s/<alias>` export converged without adding write side effects to public GET.

`fwrouter_api/services/xray_handoff.py` assigns managed egress tags/listeners for Xray handoff into Mihomo; this is an explicit path, not normal LAN transparent ingress.

For concrete server overrides, the handoff listener `proxy` target must use the Mihomo runtime proxy name (`raw._fwrouter_runtime_name`, then `raw.name`, then `server_name`) rather than the human display name. This lets restored legacy profile subjects converge when subscription server names differ from their generated Mihomo proxy names.

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
