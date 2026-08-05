# 0004: Separate Persistent And Runtime State

## Status

Accepted.

## Context

The project manages both desired intent and live kernel/container state. Mixing them breaks reboot recovery.

## Decision

Store persistent intent/state in SQLite and generated artifacts. Recreate runtime kernel state during startup recovery.

## Consequences

- Reboot recovery becomes predictable.
- Rollback and last-good discipline are easier to reason about.
- Startup recovery is mandatory system behavior.
- Agents must not treat live nftables or policy-routing state as source of truth.

## Related Files

- `/opt/fwrouter-api/fwrouter_api/core/paths.py`
- `/opt/fwrouter-api/fwrouter_api/services/bootstrap.py`
- `/var/lib/fwrouter-v2/generated/`
- `/var/lib/fwrouter-v2/last-good/`
- `/run/fwrouter-v2`
