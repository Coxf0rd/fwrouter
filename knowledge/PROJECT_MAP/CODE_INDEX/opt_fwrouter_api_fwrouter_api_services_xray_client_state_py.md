# `/opt/fwrouter-api/fwrouter_api/services/xray_client_state.py`

## Purpose

Owns local Xray client/subject state helpers split out from `xray.py`.

## Runtime Impact

Reads local Xray subject aliases, resolves a runtime client to its effective
subject, tombstones stale local subjects after adapter `not found` responses,
updates local aliases, serializes client DTOs, and triggers Xray-only inventory
sync.

## Guardrails

- Keep adapter CRUD orchestration in `xray.py`.
- Keep this module focused on local SQLite subject/inventory state.
- Preserve the `/api/v2/xray/clients/{client_id}/subscription` path shape.
