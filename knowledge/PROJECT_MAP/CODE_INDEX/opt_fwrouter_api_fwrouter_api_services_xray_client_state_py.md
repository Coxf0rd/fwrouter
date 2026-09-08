# `/opt/fwrouter-api/fwrouter_api/services/xray_client_state.py`

## Purpose

Owns local Xray client/subject state helpers split out from `xray.py`.

## Runtime Impact

Reads local Xray subject aliases, resolves a runtime client to its effective
subject, removes scoped stale/generated Xray external-client projections after
delete paths, updates local aliases, serializes client DTOs, and triggers
Xray-only inventory sync.

## Guardrails

- Keep adapter CRUD orchestration in `xray.py`.
- Keep this module focused on local SQLite subject/inventory state.
- Preserve the `/api/v2/xray/clients/{client_id}/subscription` path shape.
- Cleanup helpers must stay scoped to `implementation_kind='xray'`,
  `subject_type='explicit_external_client'`, and `subject_role='vless_client'`
  so they cannot delete shared/system subjects.
