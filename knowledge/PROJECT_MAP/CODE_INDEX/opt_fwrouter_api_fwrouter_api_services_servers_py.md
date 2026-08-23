# `/opt/fwrouter-api/fwrouter_api/services/servers.py`

## Purpose

Compatibility facade for server inventory, global routing selection, subject
server overrides, and VPN-auto membership preferences.

## Review Notes

Most behavior now lives in focused modules:

- `server_inventory.py` owns server row serialization, listing, lookup, and Mihomo inventory sync.
- `server_state.py` owns persisted global routing state helpers.
- `server_global_selection.py` owns global fixed/auto server apply and rollback.
- `server_subject_overrides.py` owns per-subject manual server overrides.
- `server_preferences.py` owns VPN-auto and global-list membership preferences.

Keep this facade stable because routes, tests, and older internal imports still
import the public service API from `fwrouter_api.services.servers`.

## Runtime Impact

The facade should not add new runtime behavior by itself. Runtime side effects
come from the delegated modules: SQLite intent updates, Mihomo/Xray reconcile,
selector updates, and global routing apply.

## Guardrails

- Keep FWRouter core as the authority for classification and policy routing.
- Keep Mihomo as a VPN egress adapter, not the network policy engine.
- Preserve direct-safe behavior for host/control-plane traffic unless an explicit scoped contour says otherwise.
- Preserve facade signatures when moving internals, especially arguments used by
  API routes and monkeypatch-based regression tests.
