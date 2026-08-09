# `/opt/fwrouter-api/fwrouter_api/services/xray_runtime_state.py`

## Purpose

Holds low-level Xray runtime-state helpers split out from `xray.py`.

## Runtime Impact

Reads the persistent Xray bindings state file, inspects generated
`config.json` outbounds, reads the `modules` row, checks whether the selected
server can be materialized for Xray egress, and updates the Xray module
runtime/apply status row.

## Guardrails

- Keep this module focused on state inspection and module-row synchronization.
- Do not put client CRUD, subscription export, or runtime materialization
  orchestration here.
- The selected-server materialization check must keep the same supported server
  shape contract used by `xray.py` reconcile/export paths.
