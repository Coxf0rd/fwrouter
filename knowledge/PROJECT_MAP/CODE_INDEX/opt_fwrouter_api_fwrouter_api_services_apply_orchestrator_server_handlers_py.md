# `/opt/fwrouter-api/fwrouter/api/services/apply/orchestrator/server/handlers.py`

## Purpose

Extracted module from the apply/Xray split. Keep this card concise and update the shared project map separately when responsibilities change.

## Notes

- Passes job attribution to the transactional assignment writer; does not
  create a second audit row for the same intent.

- Keep facade import compatibility stable.
- Preserve monkeypatch-compatible facade paths used by tests and integration code.
