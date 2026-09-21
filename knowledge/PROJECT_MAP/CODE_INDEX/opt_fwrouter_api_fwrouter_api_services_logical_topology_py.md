# `/opt/fwrouter-api/fwrouter_api/services/logical_topology.py`

## Purpose

Owns normalized logical-server members, runtime-adapter effective-member observation,
member health evidence, logical health aggregation, and bounded probe selection.

## Runtime Impact

Read projection observes runtime effective member without selecting a member. Background
observation persists the mapped effective `member_id`; member probes update
health evidence and the persisted round-robin cursor.

## Guardrails

- The active runtime adapter owns member selection and failover commands.
- `member_id` is internal identity; presentation labels are derived separately.
- Stale evidence is neither fresh healthy nor fresh failed.
- A direct member probe must not change the effective active member.
- Exact logical-server batch checks use the generic logical-group-probe-many capability when available, then import each returned snapshot sequentially through the canonical health persistence path; unsupported topology or capability falls back to single checks.
