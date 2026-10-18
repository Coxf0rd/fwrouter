# `/opt/fwrouter-api/fwrouter_api/services/core_intent.py`

## Purpose

Persistence primitives for the Core owner's monotonic intent revision, apply fencing, native runtime context and final pending-to-clean CAS.

## Guardrails

- This is not a second authority manager; desired state remains in `modules`, and apply remains owned by the canonical global apply path.
- The local revision is preserved across transfer import and never imported as provenance.
- Runtime context is read-only boot identity plus the network namespace inherited by the local nft script runner. Missing or changed context blocks Core settlement.
- SQLite read/check/write settlement uses `BEGIN IMMEDIATE` so concurrent intent writers cannot pass between validation and commit.
- Core writers and native apply owners share the existing reentrant Xray writer guard. Acquire it before SQLite and retain it across the native-effect window; the canonical apply pipeline binds every successful apply to the current local revision and rejects an explicitly stale revision before native work.
