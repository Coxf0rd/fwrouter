# `/opt/fwrouter-api/fwrouter_api/services/server_subject_overrides.py`

## Purpose

Owns per-subject manual server override persistence.

## Main Responsibilities

- Validate subject existence before writing overrides.
- Validate selectable servers for manual subject routing.
- Set, clear, read, and mark apply status for subject server overrides.

## Runtime Impact

Writes SQLite subject override intent and apply status. Runtime materialization is
handled by the surrounding apply/reconcile pipeline.

## Guardrails

- Keep manual override TTL semantics intact.
- Do not allow inactive or non-global-list servers as user-selectable overrides.
- Preserve subject taxonomy behavior for virtual VPN-auto selection.
