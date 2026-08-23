# `/opt/fwrouter-api/fwrouter_api_services_watchdog_runtime_state.py`

## Purpose

Persistence helper for the single-row `watchdog_state` table used by VPN watchdog debounce and failover cooldown logic.

## Behavior Notes

- Owns creation of the `watchdog_state` row, row-to-dict mapping, failure-candidate JSON serialization, and empty-state fallback when the table cannot be read.
- Exposes `load_watchdog_runtime_state(...)` and `update_watchdog_runtime_state(...)` so `services/watchdog.py` can keep decision logic separate from raw SQLite row handling.
- Does not decide whether traffic is failed, whether failover is allowed, or which VPN target should be selected.

## Runtime Impact

Reads and writes SQLite `watchdog_state`. It has no scheduler thread, no runtime probes, no server switching, and no log side effects.

## Guardrails

- Keep this module storage-only; watchdog policy belongs in `services/watchdog.py`.
- Preserve the tolerant empty-state fallback because watchdog startup and tests may run before runtime state has been materialized.
- Keep JSON fields deterministic with sorted keys so state diffs remain readable.
