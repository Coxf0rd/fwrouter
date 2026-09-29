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

Explicit Xray client modes are rendered inside the same scoped VLESS routing
rules: `vpn` uses its normal per-client Mihomo handoff, `disabled` is denied
with the managed blackhole outbound, and legacy `direct` gets an explicit
per-client freedom rule. A stored legacy `selective` intent is preserved and
blocked per client because its rule contract is unsupported; it is not silently
sent through the global fallback. New explicit-client mode writes accept VPN,
Disabled, and the compatibility `enabled` alias for VPN; new Direct/Selective
writes fail validation. Mode rules precede per-client VPN handoffs and are
verified against the effective generated config before the mode is considered
applied.

The synthetic profile control uses the existing subject-mode job. It resolves
the exact token-derived profile identity and only supports the canonical
single-client-account shape. Profile Disabled changes account/client enabled
flags without deleting identity or projections and reconciles away only that
profile's runtime clients. An explicit group VPN action enables the profile and
deliberately sets current member intent to VPN; ordinary profile re-enable and
inventory reconciliation preserve member mode intent. Runtime failures leave
the profile records available for retry and are reported as failed/pending,
not as applied Disabled/VPN. Existing member target-override rows, including
auto and expired selectors, remain intact during aggregate mode changes.

All Xray config/client/profile writers share a reentrant thread and process
writer guard rooted in the configured runtime directory. It serializes
intent-to-runtime profile changes, adapter mutations and config materialization
without holding an SQLite write transaction while waiting for the guard.

Xray does not select subscription concrete members. For vpn-auto or fixed
logical targets it hands traffic to Mihomo's logical runtime target; Mihomo then
selects the effective member inside that logical server. FWRouter may observe
the active member for health and diagnostics, but that member identity is not
materialized into Xray outbound selection.

Manual-only VPN-auto servers (`vpn_auto=true`, `vpn_auto_priority=-1`),
including custom proxy logical servers, remain valid fixed/manual Xray logical
targets. They must not be included in Xray automatic vpn-auto pools, and Xray
must not pin or materialize a concrete `member_id`.

Create convergence verifies that the effective Xray config contains the client in the `vless-ws` inbound, contains the expected `fwrouter-egress-*` SOCKS outbound pointing at the Mihomo handoff listener, and contains a user-scoped routing rule from `vless-ws` to that outbound. A stale rule for the same client/user that sends traffic to `fwrouter-api` is a convergence failure and must not be considered success.

Delete convergence verifies that the client is absent from the effective runtime and that managed egress/rule residue is removed or disabled by the current lifecycle. Repeat delete is safe and becomes a no-op when the runtime and local projection no longer contain the client.

## Public Subscription Profiles

`fwrouter_api/services/subscription_profiles.py` builds Clash/Mihomo, raw/base64 VLESS, and Happ payloads based on query, app, and user agent.

`fwrouter_api/services/xray_subscription.py` builds canonical VLESS URIs. Public host comes from public subscription request headers or `FWROUTER_XRAY_PUBLIC_HOST`; path and port come from `FWROUTER_XRAY_PUBLIC_PATH`/`FWROUTER_XRAY_PUBLIC_PORT`. The subscription gateway must forward the original public `Host`/proto to the API; internal loopback/private upstream hosts are never valid client endpoints and fall back to the configured public host.

Settings external-client create immediately creates the domain-visible `/s/{name}` subscription profile and runs the existing profile reconcile/materialization. Delete currently accepts only accounts with exactly one subscription client whose token equals the canonical account slug; other token cardinalities/shapes fail closed before job creation and are rechecked in the worker. It disables the exact account/client identity, removes compatibility identities, and reconciles only that profile's generated `sub-*` runtime clients/materialization. Only after successful convergence does it remove the account row; FK cascade removes its subscription clients and profile snapshots, while scoped Xray subject projections/overrides are cleaned. On convergence failure, account/client/snapshot/projection rows remain for retry, but their enabled flags may already be disabled. Job results contain only hashed refs and bounded counts, not slug/token/email/UUID. Audit events remain redacted; `jobs.input_json` raw token storage remains a separate security follow-up.

Settings aggregate delete uses the exact non-secret `subscription-account:<account_id>` inventory reference for enabled and disabled profiles. The backend resolves that reference strictly and carries the account ID through the worker; execution revalidates the same ID and slug so a stale queued job cannot delete a recreated account with the same slug. Malformed, unknown, or stale references fail closed. Legacy token/slug callers remain supported by resolving to an existing account before job creation. Repeated DELETE after hard deletion returns not-found and creates no job.

Public subscription GET is read-only. It does not create DB identities, run reconcile, or materialize runtime. When the managed Xray module is enabled, the renderer exports only nodes that are currently runtime-exportable: the effective Xray config must contain the VLESS client, `fwrouterBinding`, a scoped `vless-ws` user rule to `fwrouter-egress-*`, and no stale user-specific `fwrouter-api` fallback.

Public profile responses retain protocol headers (`Subscription-Userinfo`, `Profile-Title`, `Profile-Update-Interval`) and `Cache-Control: no-store`. They do not echo the subscription token or expose FWRouter diagnostic/count/renderer headers. `Profile-Update-Interval: 1` is a client polling hint independent of the four-hour provider inventory timer.

Subscription refresh and startup reconcile use one staged generation for profile and generated `vpn-auto-*` clients. The service allocates provisional UUIDs in memory, builds prospective subjects/bindings and exact handoff assignments, and native-validates both Xray and transition/final Mihomo candidates before inventory reconciliation or pruning. It applies the transition Mihomo config, exact validated Xray bytes, then final Mihomo config; it verifies full managed UUID/email and binding/mode parity before publishing bindings and runtime-verified snapshots. A failure restores the outer last-good runtime/artifacts and scoped generated Xray projections while preserving independent account, routing, and user-mode intent.

An unresolved private checkpoint under the Xray runtime `.generation/` directory is recovered before module/startup reconciliation and vpn-auto cleanup. Recovery compares scoped source intent and derived-row postimages; a conflict keeps the marker and reports pending/degraded rather than claiming success. Native validators and runtime I/O run without an open SQLite write transaction. Refresh runs the combined Xray generation before its ordinary Mihomo reconciliation; the latter is not used as a second apply window after publication. VPN-auto selector changes remain separate routing intent and run after the new target inventory is applied.

SQLite publication and checkpoint fsync cannot form one cross-filesystem transaction. If the process stops after a scoped DB commit but before its checkpoint postimage is recorded, recovery restores the captured runtime but retains the checkpoint as pending because it cannot prove which projection rows belong to the generation. Status remains degraded for operator investigation; recovery never guesses or removes the marker.

The Xray status projection checks the complete managed identity set (`sub-*` and `vpn-auto-*`) in the active VLESS inbound against applied bindings and mode directives. Extra or missing managed identities prevent forced-VPN readiness; unreadable active evidence is unknown. Unrelated standalone identities and per-client health remain independent.

Profile node alias and subject-server override materialization is batched in one
transaction with one schema inspection. Existing semantic values are preserved
without timestamp churn; missing subjects retain the existing failure contract.

`fwrouter_api/services/xray_handoff.py` assigns managed egress tags/listeners for Xray handoff into Mihomo; this is an explicit path, not normal LAN transparent ingress.

For concrete server overrides, the handoff listener `proxy` target must use the Mihomo runtime proxy name (`raw._fwrouter_runtime_name`, then `raw.name`, then `server_name`) rather than the human display name. This lets restored legacy profile subjects converge when subscription server names differ from their generated Mihomo proxy names.

## UI Read Model

Public subscription profile nodes may create multiple real `explicit_external_client` subject rows for one logical client. UI/read-model aggregates them into synthetic `xray-subscription:sub-<token-digest>` subjects, independent of the display name and without exposing the token in that ID. Existing label-based group IDs still resolve for compatibility. An enabled profile remains in Settings inventory without recent traffic, but is not reported online without activity evidence. Runtime/accounting detail rows such as `sub-*` and service clients are hidden from normal user lists when they would create duplicate/noisy rows.

Ordinary auto-subscription endpoints retain auto eligibility (`vpn_auto=1`, active, not manually deleted, priority at least zero). A manual-only custom HTTPS proxy is also an explicit VLESS subscription endpoint; this does not add it to automatic selector/watchdog pools. Public `/s` still exports that endpoint only after runtime verification and snapshot promotion.

Settings inventory exposes explicit-client `supported_admin_modes` and
`mode_support_state` so legacy Direct/Selective intent remains visible without
being converted on read. Aggregate profile mode uses its persisted enabled
flag; disabled profiles remain distinct from the retained member mode used if
the profile is later re-enabled. VPN-auto selection projections keep logical
server IDs separate from runtime selector targets and from the effective fixed
global route. `auto_transition.changed` is asserted only from exact runtime
selector readback, while effective-route change is reported independently.
The current verified auto target can include allowlisted reason/source and a
safe matched server label; caller attribution remains unverified.

Managed server preference edits that change the eligible VPN-auto target set
record a durable pending revision. Existing scheduler ticks dispatch it after
the trailing debounce; explicit `POST /api/v2/xray/vpn-auto/reconcile` queues
the same existing reconciliation immediately. `GET /api/v2/xray` includes a
read-only `vpn_auto_reconcile` status object. Completion applies only to the
claimed revision after final runtime and public-profile verification; a newer
revision remains pending.

## Boot Relevance

- The configured Docker network must exist before service start.
- Generated config must exist in persistent state.
- API must be ready before the subscription gateway starts.

## Risks

- The Xray unit does not create the Docker network itself. The installer creates it only when a managed runtime component is selected.
- Gateway depends on API readiness rather than direct Xray readiness.
- `latest` images increase nondeterministic runtime behavior risk.
- Non-idempotent config writes can restart clients during accounting/polling.
