# Backend

FastAPI control-plane for FWRouter.

## Monorepo Role

- API routes under `fwrouter_api/routes/`
- service orchestration under `fwrouter_api/services/`
- job manager and handlers under `fwrouter_api/jobs/`
- SQLite schema/state access under `fwrouter_api/db/`
- backend tests under `tests/`
- deployed app path: `/opt/fwrouter-api`

## Does Not Own

- live kernel `nftables` state directly; privileged operations go through host libexec scripts
- secrets; live `/opt/fwrouter-api/.env` is host-local
- generated runtime state in `/var/lib/fwrouter-v2`

## Local Runtime

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -e .
.venv/bin/uvicorn fwrouter_api.main:app --host 127.0.0.1 --port 5000
```

## Control-Plane Scope

This service owns:

- SQLite state under `/var/lib/fwrouter-v2/fwrouter.db`
- backend API under `/api/v2`
- job orchestration
- operational logs
- integration adapters for dataplane, Mihomo, Xray/VLESS, rules, subscription, watchdog and selector

The UI layer must remain a static/proxy/adapter layer and must not own system or business logic.

## Current Status

Implemented well:

- API/control-plane skeleton under `/api/v2`
- SQLite-backed modules, jobs, logs, subscription state, server inventory
- Mihomo candidate/promote flow
- selector / watchdog / server-ping control-plane logic
- Tailscale module control-plane enable/disable flow with host status probe and `tailscale_node` inventory sync
- subject effective-state and override control-plane logic
- manifest-based dataplane contract generation
- `Bypass core` as persistent backend/runtime state
- `scoped egress v1` for `lan` and `tailscale_node`
- `global selective` runtime enforcement for effective IP/CIDR-only rulesets with honest preflight boundaries
- external rules-source fetch for `big_direct` / `big_vpn` through env-configured backend URLs
- custom HTTPS proxy servers stored by backend and rendered alongside regular server inventory
- control-plane transfer snapshot export/validate/plan/import workflow

Not claimed as complete without target-host verification:

- final Linux-side verification of host Tailscale lifecycle actions and subject import
- final live Linux acceptance for `global direct`, `global vpn`, `Bypass core` and `scoped egress`
- exact live traffic attribution for every subject class beyond the current collector path
- final watchdog production signal validation on the live host

## Deployment Notes

- This tree is a deployable source component, not a live server dump.
- Runtime snapshots, backups, `.venv`, `__pycache__`, and old job artifacts are intentionally excluded.
- Copy `.env.example` to `/opt/fwrouter-api/.env` on the Linux host before starting `fwrouter-api.service`.
- Configure remote rules list URLs in `.env` when using backend-driven `rules/full-update`.
