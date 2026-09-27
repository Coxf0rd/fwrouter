# `/opt/fwrouter-api/fwrouter/api/services/apply/orchestrator/results.py`

## Purpose

Extracted module from the apply/Xray split. Keep this card concise and update the shared project map separately when responsibilities change.

## Notes

- Avoids duplicate audit rows already emitted at persistence points; if
  committed mode/assignment intent later fails to apply, emits a separate
  correlated operational failure.
- If a committed intent has no matching atomic audit row, emits the operational
  `admin.audit_event_missing` diagnostic; it does not create a post-commit audit.

- Keep facade import compatibility stable.
- Preserve monkeypatch-compatible facade paths used by tests and integration code.

Manual-rule apply operational journal records use an allowlist (safe hash/counts, stable error code, phase, and correlation); nested rule text and provider payloads are not persisted. The job/API result remains unchanged.
