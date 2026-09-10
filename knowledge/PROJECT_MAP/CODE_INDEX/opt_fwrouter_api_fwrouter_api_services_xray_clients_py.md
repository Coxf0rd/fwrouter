# `/opt/fwrouter-api/fwrouter/api/services/xray/clients.py`

## Purpose

Extracted module from the apply/Xray split. Keep this card concise and update the shared project map separately when responsibilities change.

## Notes

- Keep facade import compatibility stable.
- Preserve monkeypatch-compatible facade paths used by tests and integration code.
- UI/API create and delete routes submit bounded background jobs through the
  shared job manager. The synchronous CRUD helpers remain the worker/internal
  implementation.
- Create uses an identity lock keyed by email/alias so a repeated logical
  request returns the active job or the existing client instead of creating a
  duplicate.
- `delete_xray_client(...)` removes the runtime client, syncs Xray inventory,
  deletes scoped local Xray subject projections/overrides for that client, then
  materializes runtime bindings and emits the external-client lifecycle event.
