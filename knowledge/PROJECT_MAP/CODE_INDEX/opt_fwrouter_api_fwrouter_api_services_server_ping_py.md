# `/opt/fwrouter-api/fwrouter_api/services/server_ping.py`

## Purpose

Canonical backend ping core for servers. It resolves stable `server_id` values
to Mihomo runtime targets, probes delay through the Mihomo adapter, and
persists source-aware observations in `server_ping_state`.

## Important Functions

- `resolve_server_runtime_target(server_id)`
  Accepts the persistent server identity and returns the runtime proxy target.
  Subscription servers use `_fwrouter_runtime_name`/raw `name`; custom proxy
  servers use the custom `server_name`.
- `record_ping_result(...)`
  Writes the canonical ping DTO fields: `server_id`, `runtime_target`, `source`,
  `status`, `latency_ms`, `checked_at`, `error_code`, and `error_message`.
- `get_server_ping_state(server_id)`
  Returns separate manual/presentation and runtime/background observations.
- `get_recent_runtime_ping_success(server_id, ttl_seconds=...)`
  Returns a fresh successful runtime/background observation for watchdog
  quality checks without consulting manual presentation state.
- `check_server_delay(server_id, ...)`
  Accepts persistent `server_id`, resolves the Mihomo runtime target, probes the
  runtime proxy, and writes the result back to the same persistent `server_id`
  when `update_state=True`.
- `check_active_server_delay(...)`
- `check_server_delay_sweep(...)`

## Runtime/Persistent State

- Writes `server_ping_state` only when explicitly requested.
- The existing `status`/`last_ping_ms` columns remain the runtime/background
  observation used by selector/watchdog.
- Manual observations are stored in `manual_*` columns. A later
  background/selector/watchdog failure must not erase the last manual result;
  the next manual failure replaces it with an explicit manual error.
- User/Admin presentation reads the manual lane through server inventory
  projection. Runtime automation reads the runtime/background lane.
- `source` is normalized to one of `manual`, `selector`, `watchdog`, or
  `background` in service DTOs.
- Custom proxy runtime target is the custom display/server name.
- Subscription server runtime target is `_fwrouter_runtime_name` or raw `name`
  from `servers.raw_json`, not the stable `sub:<hash>` ID.

## Guardrails

- Do not treat display name as identity.
- Do not persist ping results under Mihomo runtime names.
- Keep the stored ping value keyed by stable `server_id` so selector, UI, and
  migration references remain coherent.

## Boot Persistence Relevance

Low/medium. Important for selector/watchdog/operator diagnostics, but not part
of the base boot dataplane contract.
