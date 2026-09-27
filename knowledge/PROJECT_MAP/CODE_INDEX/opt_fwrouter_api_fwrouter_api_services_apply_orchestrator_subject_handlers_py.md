# `/opt/fwrouter-api/fwrouter/api/services/apply/orchestrator/subject/handlers.py`

## Purpose

Extracted module from the apply/Xray split. Keep this card concise and update the shared project map separately when responsibilities change.

## Notes

- Passes actor and job context to the commit-stage audit writer; runtime
  results do not duplicate the committed intent event.

- Keep facade import compatibility stable.
- Preserve monkeypatch-compatible facade paths used by tests and integration code.
