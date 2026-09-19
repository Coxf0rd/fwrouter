# `/opt/fwrouter-api/fwrouter_api/services/logical_topology.py`

## Purpose

Owns normalized logical-server members, Mihomo effective-member observation,
member health evidence, logical health aggregation, and bounded probe selection.

## Runtime Impact

Read projection observes Mihomo `now` without selecting a member. Background
observation persists the mapped effective `member_id`; member probes update
health evidence and the persisted round-robin cursor.

## Guardrails

- Mihomo owns member selection and failover.
- `member_id` is internal identity; presentation labels are derived separately.
- Stale evidence is neither fresh healthy nor fresh failed.
- A direct member probe must not change the effective active member.
