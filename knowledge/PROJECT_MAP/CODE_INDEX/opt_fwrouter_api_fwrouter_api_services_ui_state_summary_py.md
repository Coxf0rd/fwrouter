# `/opt/fwrouter-api/fwrouter_api/services/ui_state_summary.py`

## Purpose

Owns router summary and settings workspace DTOs.

## Main Responsibilities

- Build cached router summary with global mode, server mode, current server name, router self subject, and active apply job.
- Build cached settings workspace with display systems, modules, subscription, traffic, Xray status, counts, and recent logs.

## Runtime Impact

Reads SQLite state and service summaries through short TTL caches. It should not
write persistent state or trigger runtime apply.

## Guardrails

- Keep operational and technical logs summarized through `ui_state_logs.py`.
- Keep workspace summary as an aggregation layer; detailed runtime health belongs in dedicated runtime endpoints.
