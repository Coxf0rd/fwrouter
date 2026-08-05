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
