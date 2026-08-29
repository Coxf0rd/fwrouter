# `/opt/fwrouter-api/fwrouter_api/services/ui_state_logs.py`

## Purpose

Formats operational and technical logs for the UI after the split from
`ui_state.py`. It owns compact operator details and UI visibility decisions for
log rows, while shared localized titles/reasons live in `ui_text.py`.

## Runtime Impact

Most functions are pure formatting over already-loaded event dictionaries.
`summarize_ui_log_events()` may read the current routing/dataplane state to hide
already resolved startup-recovery transient failures from the default UI view.

## Guardrails

- Keep raw logs complete in storage; this module only shapes the compact UI DTO.
- Resolved startup-recovery and runtime-convergence scheduler warning/error storms
  may be hidden from `ui_only=true` views only after current routing is `clean`
  and live dataplane matches persisted intent; unresolved recovery failures must
  remain visible. Correlated `apply_failed` events may be hidden only when their
  `job_id`/`apply_id` matches an already identified resolved recovery failure.
- Keep `_summarize_log_event` re-exported from `ui_state.py` while routes/tests use the old import path.
- Do not show large raw dumps, apply IDs, job IDs, or capability payloads in the default operator-facing log view.
- Keep reusable operator-facing text in `ui_text.py`. This module may keep only log-specific labels and compact event detail formatting.
- UI-visible logs should show state changes, user actions, recovery, and real problems. Successful internal candidate/write/validate steps remain in raw technical logs but are hidden from the ordinary UI journal unless they become warning/error events.
- Watchdog technical events stay complete in JSONL storage, but no-op suppression statuses such as stale signal, pending confirmation, healthy response traffic, disabled/paused watchdog, and missing module are hidden from the UI journal and should not be treated as warnings. Real watchdog problems and failover/recovery actions remain visible.
- `watchdog_switch_applied` rows must have a localized operator-facing message for `failover_applied`; do not let raw English backend diagnostics appear as the primary UI log text.
- UI log DTOs include a compact `category`. `vpn_auto_server_switched` is categorized as `watchdog` only when raw details show `requested_by`/`reason` came from watchdog failover automation; manual/external selector switches remain `server`.
- Watchdog UI summaries use compact translated fields such as Status, Reason, Action taken, and Code instead of raw `watchdog.status.*` or `switch_allowed` strings.
- Common backend failure codes such as `RULES_VALIDATION_FAILED` get a short operator-facing reason in the UI DTO even when the stored diagnostic message is English.
- Xray warning/error log rows use stable event-type messages and reasons in both Russian and English; do not fall back to raw adapter diagnostics as the primary UI message/reason when a known event key exists.
- Legacy/manual `vpn_watchdog_*` events must be mapped to the same localized watchdog messages and status reasons as the newer `watchdog_*` events.
- Mihomo technical validation warnings use stable technical event messages/reasons, not raw validation diagnostics as the primary UI text. Successful `mihomo_candidate_config_written` and `mihomo_candidate_config_validated` info events are diagnostic-only and must not appear in `ui_only=true` journal responses.
- Generic log-detail truncation, boolean labels, count labels, and fallback event titles must honor the requested locale through locale maps. Add a new language by extending those maps instead of adding language-specific conditionals.
- The primary UI `message` field is intentionally compact for both known event titles and unknown/raw backend diagnostics. Known watchdog rows keep the event-list title to the short action name; status, reason, progress and diagnostics stay in localized details, while long raw messages are preserved separately as `diagnostic_message`.
