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
- Keep operator-facing text in `UI_TEXT_REGISTRY`: stable namespaces such as `watchdog.status`, `watchdog.action`, `error.code`, `traffic.metric`, `inventory.activity`, `display.system.*`, `connection.*`, and `server.virtual` map backend machine keys to `title`/`reason` strings. Registry entries are locale-aware through `title_i18n`/`reason_i18n`; default output remains Russian unless callers pass another supported locale. Unknown keys must use a localized fallback and keep the raw machine code in details for diagnostics.
- Watchdog technical events stay complete in JSONL storage, but their UI DTO must use operator-facing messages/details such as why a server switch was suppressed. Existing `paused_signal_unavailable` watchdog rows are hidden from the UI because no fresh VPN traffic can be normal idle state, not an operator action item.
- `active_quality_degraded_traffic_healthy` watchdog rows must explain that the current-server delay-check is degraded while real VPN response traffic is still healthy, so no server switch was applied.
- UI log DTOs include a compact `category`. `vpn_auto_server_switched` is categorized as `watchdog` only when raw details show `requested_by`/`reason` came from watchdog failover automation; manual/external selector switches remain `server`.
- Watchdog UI summaries use compact fields like `Статус`, `Причина`, `Что сделано`, and `Код` instead of raw `watchdog.status.*` or `switch_allowed` strings.
- Common backend failure codes such as `RULES_VALIDATION_FAILED` get a short operator-facing reason in the UI DTO even when the stored diagnostic message is English.
