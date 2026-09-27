# `/opt/fwrouter-api/fwrouter/api/services/apply/orchestrator/rules/handlers.py`

## Purpose

Extracted module from the apply/Xray split. Keep this card concise and update the shared project map separately when responsibilities change.

## Notes

- Keep facade import compatibility stable.
- Preserve monkeypatch-compatible facade paths used by tests and integration code.

On successful apply, compares the normalized candidate with the prior active set and passes hash/count-only audit metadata to the SQLite success transaction. Failed applies do not produce a successful-intent audit.
