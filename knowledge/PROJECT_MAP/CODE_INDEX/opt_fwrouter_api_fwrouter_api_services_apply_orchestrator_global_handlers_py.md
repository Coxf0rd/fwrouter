# `/opt/fwrouter-api/fwrouter/api/services/apply/orchestrator/global/handlers.py`

## Purpose

Extracted module from the apply/Xray split. Keep this card concise and update the shared project map separately when responsibilities change.

## Notes

- Keep facade import compatibility stable.
- Preserve monkeypatch-compatible facade paths used by tests and integration code.

Passes caller/job/apply context to persistent routing commits so audit records represent changed administrator intent, not runtime convergence.
