# `/opt/fwrouter-api/fwrouter_api/services/external_collectors.py`

## Purpose

Safe runner for external connection collectors. Used for systems that cannot
push updates into the FWRouter API themselves.

## Runtime Impact

- `run_external_connection_collector(...)` runs one collector manually/API:
  `api_push` returns skipped, `http_poll` reads JSON from a URL,
  `command_probe` runs only an allowlisted `script_id`, and `file_read` reads
  JSON below `/var/lib/fwrouter-v2/external-collectors/`.
- `run_due_external_collectors_once(...)` runs only enabled custom external
  connections with `refresh_mode=interval`, respecting
  `collector_config.interval_seconds`.
- `start_external_collector_scheduler(...)` / `stop_external_collector_scheduler(...)`
  attach interval collectors to backend startup/shutdown.

## Guardrails

- Do not accept arbitrary shell commands from UI. `command_probe` must stay
  allowlist-based.
- Successful interval ticks should not be logged; failures are deduped
  technical logs.
- Traffic samples are applied only with `collector_config.apply_traffic=true`
  and `dry_run=false`.
- This module does not auto-import `clients` into `subjects`; that requires a
  separate provider contract because it affects routing.
