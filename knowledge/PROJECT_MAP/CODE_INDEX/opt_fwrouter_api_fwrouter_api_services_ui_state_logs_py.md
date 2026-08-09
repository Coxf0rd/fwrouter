# `/opt/fwrouter-api/fwrouter_api/services/ui_state_logs.py`

## Purpose

Formats operational and technical logs for the UI after the split from
`ui_state.py`. It owns localization, compact operator details, and UI visibility
decisions for log rows.

## Runtime Impact

This module is pure formatting over already-loaded event dictionaries. It does
not read SQLite, dataplane, runtime probes, or persistent settings.

## Guardrails

- Keep raw logs complete in storage; this module only shapes the compact UI DTO.
- Keep `_summarize_log_event` re-exported from `ui_state.py` while routes/tests use the old import path.
- Do not show large raw dumps, apply IDs, job IDs, or capability payloads in the default operator-facing log view.
