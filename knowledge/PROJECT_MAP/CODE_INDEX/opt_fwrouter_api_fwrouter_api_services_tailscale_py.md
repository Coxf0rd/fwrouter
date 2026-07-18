# `/opt/fwrouter-api/fwrouter_api/services/tailscale.py`

## Назначение

Host-probe и lifecycle wrapper для Tailscale через allowlisted scripts.

## Важные функции

- `probe_tailscale_runtime()`
- `_probe_tailscale_runtime_uncached()`
- `run_tailscale_lifecycle_action(action)`

## Внешние зависимости

- script runner `tailscale_status/start/stop/restart`

## Runtime/persistent state

- probe read-only
- lifecycle action может менять host Tailscale runtime

## Boot persistence relevance

Низкая/средняя для core boot, но важна для tailscale module diagnostics and subject import.
