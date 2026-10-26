# `backend/fwrouter_api/services/maintenance_lifecycle_admission.py`

Supported process admission for the existing controlled backend lifecycle.
Shared process leases cover API startup/preflight, API worker lifetime and
standalone supported tools/installers. Offline rebuild requires a live exclusive
lease plus the matching durable lifecycle operation; a request flag, stopped-unit
observation or caller attribution is not authority. Process/thread identity and
revocation prevent copied/stale contexts from retaining the grant.

An interrupted maintenance checkpoint blocks startup until explicit validated
forward completion. The protocol does not exclude arbitrary root SQL, unsupported
old binaries or hand-edited service units. Deployment requires a coherent
participating-version floor, not merely installation of this Python module.

See [restore contract](../../../docs/CONTROL_PLANE_RESTORE_CONTRACT.md),
`backend/tests/test_offline_maintenance_admission.py` and
`installer/controlled_backend_lifecycle.py`.
