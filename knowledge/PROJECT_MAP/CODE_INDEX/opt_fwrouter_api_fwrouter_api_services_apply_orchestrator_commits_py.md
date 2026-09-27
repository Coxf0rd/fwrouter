# `/opt/fwrouter-api/fwrouter/api/services/apply/orchestrator/commits.py`

## Purpose

Extracted module from the apply/Xray split. Keep this card concise and update the shared project map separately when responsibilities change.

## Notes

- Admin desired-mode staging writes `client.mode_changed` in the same
  transaction as the committed desired mode; runtime application is separate.

- Keep facade import compatibility stable.
- Preserve monkeypatch-compatible facade paths used by tests and integration code.

Changed global mode, server mode, and selective-default SQLite commits also write their typed INFO/audit event in the same transaction. Equality/no-op commits do not write an audit row; runtime apply result is logged separately.
