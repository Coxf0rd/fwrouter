# Runtime Flow

## Manual Startup

1. Administrator runs install/setup or starts systemd units.
2. `fwrouter-api.service` runs core host preflight and starts `uvicorn` after `network-online.target`.
3. Optional managed runtimes such as `fwrouter-mihomo.service` and `fwrouter-xray.service` run runtime preflight checks and start containers when installed/enabled.
4. FastAPI startup executes `bootstrap_backend()`.
5. Backend creates state/log/runtime directories, initializes SQLite, cleans stale jobs, syncs subjects, and restores live routing contour when needed.

## `fwrouter-api.service` Startup

1. `ExecStartPre=/usr/local/libexec/fwrouter/fwrouter-boot-preflight.sh`.
2. Preflight checks `/dev/net/tun`, `nft`, `ip`, state directories, `rt_tables.d`, and sysctl.
3. `uvicorn fwrouter_api.main:app` starts.
4. FastAPI startup calls `bootstrap_backend()`, registers job handlers, starts maintenance, starts watchdog, and starts runtime convergence.

Before startup recovery completes, host traffic is in temporary direct-safe bootstrap mode. This is expected and must be visible in status rather than treated as an unknown routing mode.

## Restart And Reload

Backend restart can leave live kernel dataplane running while selector state or SQLite reporting drifts. Startup recovery restores Mihomo selector state and reapplies intended routing when live mode differs from persisted intent.

After startup/apply, backend may best-effort build precompiled global profiles for `direct`, `selective`, and `vpn`. This does not change live state; it reduces later global mode switch latency when stamps are fresh.

## Stop

- `fwrouter-api.service` stops backend and internal schedulers.
- `fwrouter-mihomo.service` and `fwrouter-xray.service` stop Docker containers.
- Live nftables and policy-routing state are not cleared directly by stop units; backend apply/rollback logic owns that state.

## Runtime Convergence

`runtime_convergence_scheduler` periodically checks drift. It should use a lightweight DNS selective status probe first. If selective status is healthy, it records `dnsmasq.skipped=true` and `preflight_action=skip_reconcile_status_ok` instead of running heavy `dnsmasq` reconcile. If status is unhealthy or the probe fails, it can run full DNS/rules reconcile.

## Runtime Health

The active `vpn_dataplane` registration declares logical-group state and refresh
capabilities. Core obtains operations through `runtime_adapter_operations()`.
When native state and refresh are both available, manual ping, Ping All,
background coverage, effective-member observation, and selector checks import
normalized runtime evidence. Otherwise those paths use the local delay-probe
fallback. They do not run both probe backends for the same runtime path.

Imported member evidence preserves the runtime result timestamp and records the
adapter, evidence source, probe reason, and lane in `evidence_json`. Logical
latency is valid only for the freshly observed effective member.

The 60-second active observation imports only the runtime effective member;
the bounded member scheduler owns broad member inventory refresh. Passive state
observation does not update group probe outcome. Explicit group probes persist
their latest `success`, `timeout`, `transport_error`, or `runtime_missing`
outcome separately from member health. A failed group request never synthesizes
individual member failures. Topology projections include health reason, source,
timestamp, freshness, state breakdown, and latest group probe outcome.

Selector ranking uses fresh canonical effective-member health and latency when
that evidence exists. Existing `server_ping_state` remains a fallback only when
canonical topology/evidence is absent; manual-only checks cannot qualify an
automatic candidate. Priority weights and on-demand checks keep their existing
behavior.

## VPN-auto Selection Ownership (Source Checkpoint)

Core owns the global `vpn-auto` selector target, `routing_global_state.active_auto_server_id`, and selection provenance. Watchdog and manual recovery provide a revision-bound decision snapshot; they do not write selection state directly. Core snapshots intent, eligibility, active/provenance, and runtime identity, then performs bounded network probes outside the shared writer guard. It reacquires the guard, revalidates the snapshot and candidate path, applies and reads back the exact target, and commits selection with a revision CAS.

If that persistence CAS misses after an exact runtime readback, Core releases the guard and takes one fresh observation snapshot. It may persist the observed target only when current Auto intent, eligibility, runtime generation, and the original confirmed selection still match; this repair performs no runtime PUT or latency probe and advances the revision with its own CAS. A newer confirmed selection or any changed snapshot leaves the request deferred, without adopting the foreign revision or replaying the old target. The selector result marks the first PUT readback as no longer current until reconciliation confirms it, and reports whether the reconciliation was confirmed or deferred.

`routing.auto_selection_revision` is a monotonic local state fence, separate from each operation UUID and excluded from generated-config fingerprints. Eligibility or runtime-generation mutations fence their local write; ordinary probe/cache telemetry and confirmed no-ops do not advance it. Refresh and provider GETs run outside the guard. Local inventory, allowlisted provider observation, and refresh metadata publication use a guarded phase that rejects superseded source snapshots. Provider GET handoffs carry only nonserialized identity/receipt metadata; provider credentials and connection material are not part of the handoff token. Stale or busy watchdog evidence is deferred, not treated as a health failure or recovery exhaustion.

Generation/publication rollback is conditional on the operation's owned revision and runtime generation. If a newer operation has won, stale restore declines before artifact writes or restart and leaves reconciliation to Core. Xray fixed-binding intent and provider-internal member ownership are unchanged.

Online control-plane database rebuild fails before backup/unlink when the existing database contains the local selection revision/provenance or an active Auto target (`DATABASE_REBUILD_SELECTION_FENCE_REQUIRED`). The monotonic fence cannot be imported from an older snapshot; an offline/atomic migration is deferred. Before any deployment step that can restart or replace a Mihomo generation, preflight must confirm the managed container ID plus `StartedAt` are readable. Reconcile and production selector apply fail closed when runtime incarnation is unavailable; test mocks do not satisfy this deployment preflight.

This is a source and isolated-test checkpoint only. The change is not deployed or live-verified; production startup, selector stability, and race absence remain unverified.

## Failure Paths

Mihomo failure: backend controller checks fail, selector restore is skipped, and runtime/apply paths may mark transparent contour not ready.

Xray failure: subscription gateway and client bindings depend on API plus Xray runtime config. Missing the configured Docker network blocks unit startup before the container starts.

Dataplane failure: `dataplane-check.sh` validates candidate/live contract. `dataplane-apply.sh` rebuilds the owned table and policy-routing state. `dataplane-rollback.sh` removes the owned table and restores last-good snapshot when available.

## Readiness Checks

- `/dev/net/tun` before Mihomo startup
- `network-online.target` and Docker readiness
- Mihomo controller `127.0.0.1:5200`
- API `127.0.0.1:5000` before subscription gateway
- Docker network `FWROUTER_DOCKER_PROXY_NETWORK` before Xray startup; default `fwrouter_proxy`

## Risks

- Treating routing DB state as direct before recovery can lose selective/vpn intent.
- Writing generated artifacts without promote/last-good discipline breaks rollback.
- Rebuilding heavy DNS/rules paths every minute creates avoidable load and service churn.
- Stale precompiled profiles must silently fall back to full rebuild.
