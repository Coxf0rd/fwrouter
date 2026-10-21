# `/opt/fwrouter-api/fwrouter_api/services/server_subject_overrides.py`

## Purpose

Owns per-subject manual server override persistence.

## Main Responsibilities

- Validate subject existence before writing overrides.
- Validate selectable servers for manual subject routing.
- Set, clear, read, and mark apply status for subject server overrides.
- Reconcile stale override reporting state when an authoritative runtime binding
  is already applied.

## Runtime Impact

Effective assignment set/clear operations emit `server.assignment_changed`
within the override SQLite transaction and correlate the row to the apply job.
Runtime application remains separate.

Writes SQLite subject override intent and apply status. Runtime materialization is
handled by the surrounding apply/reconcile pipeline. Xray runtime materialization
may call back into this service to clear stale pending/error reporting once the
runtime binding has been applied.

## Guardrails

- Keep manual override TTL semantics intact.
- Do not allow inactive or non-global-list servers as user-selectable overrides.
- Preserve subject taxonomy behavior for virtual VPN-auto selection.

## Typed target persistence (schema27 source)

The existing row owns explicit virtual Xray VPN-auto intent as well as real-server assignments. A discriminator plus NULL physical server FK encodes virtual intent; `subject_server_override_codec.py` projects the stable virtual ID for API/policy consumers. Historical NULL real-server rows remain distinct. Set/clear/audit compare semantic targets, retaining TTL renewal and apply-error reporting. Virtual POST is not a canonical clear operation. Admission checks the registered implementation as well as subject taxonomy. Migration and native qualification are separate from production deployment.
