# `/opt/fwrouter-api/fwrouter_api/services/server_ping.py`

## Purpose

Measures server delay through the Mihomo adapter and optionally persists the
latest result in `server_ping_state`.

## Important Functions

- `check_server_delay(server_id, ...)`
  Accepts persistent `server_id`, resolves the Mihomo runtime target, probes the
  runtime proxy, and writes the result back to the same persistent `server_id`
  when `update_state=True`.
- `check_active_server_delay(...)`
- `check_server_delay_sweep(...)`

## Runtime/Persistent State

- Writes `server_ping_state` only when explicitly requested.
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
