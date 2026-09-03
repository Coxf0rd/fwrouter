# `/opt/fwrouter-api/fwrouter/api/services/xray/materialize.py`

## Purpose

Extracted module from the apply/Xray split. Keep this card concise and update the shared project map separately when responsibilities change.

## Notes

- Keep facade import compatibility stable.
- Preserve monkeypatch-compatible facade paths used by tests and integration code.
- After successful runtime binding writes, reconcile `subject_server_overrides`
  reporting state for bindings that are actually `applied`.
