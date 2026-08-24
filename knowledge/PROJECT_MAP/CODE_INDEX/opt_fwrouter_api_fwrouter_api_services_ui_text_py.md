# `/opt/fwrouter-api/fwrouter_api/services/ui_text.py`

## Purpose

Central locale-aware registry for backend-provided UI text.

## Runtime Impact

Pure in-process helpers. The module does not read SQLite, dataplane state,
runtime probes, or persistent settings.

## Guardrails

- Put reusable operator-facing titles and reasons here, not in individual UI
  read-model modules.
- Keep Russian and English variants beside the same stable machine key through
  `title_i18n` and `reason_i18n`.
- Keep unknown-key fallbacks localized and preserve raw machine codes in caller
  details for diagnostics.
- Watchdog status keys from both current and legacy/manual flows belong in
  `watchdog.status`; do not let old `vpn_watchdog_*` events fall back to raw
  English diagnostics.
- Log-specific field labels and compact event shaping remain in
  `ui_state_logs.py`; shared namespaces such as `watchdog.status`,
  `watchdog.action`, `watchdog.event`, `error.code`, `traffic.metric`,
  `inventory.activity`, `display.system.*`, `connection.*`, and
  `server.virtual` belong here.
