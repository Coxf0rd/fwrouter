# `/opt/fwrouter-api/fwrouter/api/services/apply/orchestrator/global/handlers.py`

## Purpose

Extracted module from the apply/Xray split. Keep this card concise and update the shared project map separately when responsibilities change.

## Notes

- Keep facade import compatibility stable.
- Preserve monkeypatch-compatible facade paths used by tests and integration code.

Passes caller/job/apply context to persistent routing commits so audit records represent changed administrator intent, not runtime convergence.

Same-mode routing no-op remains available for clean Core state. Pending Core enable flows through the existing global apply owner, binding the captured Core revision to the owned apply result; route commit CAS and fresh native proof settlement prevent stale work from publishing or clearing newer intent.
