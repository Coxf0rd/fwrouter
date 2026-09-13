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
- Create job success is gated by runtime convergence, not only adapter success:
  the effective Xray runtime must contain the client identity and must not have
  a stale user rule pointing at `fwrouter-api`.
- `delete_xray_client(...)` removes the runtime client, syncs Xray inventory,
  deletes scoped local Xray subject projections/overrides for that client, then
  materializes runtime bindings and emits the external-client lifecycle event.
- Delete job success is gated by absence of the client from effective runtime;
  repeated delete is a safe no-op after the client/projection is already gone.
