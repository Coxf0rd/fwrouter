# `backend/fwrouter_api/services/control_plane_import_admission.py`

## Purpose

Enforces the fixed, maintenance-only admission contract for control-plane
snapshot import. The verifier is root-owned host policy at
`/etc/fwrouter/maintenance-import-verifier.json`; no environment override or
automatic provisioning path exists.

## Important Functions

- `_secure_verifier_bytes()` opens the fixed path without following symlinks,
  enforces owner/mode/size and checks stable file identity while reading.
- `_verify_request()` validates the exact loopback/Host/header/content-type
  shape and compares a bearer-token digest in constant time.
- `require_control_plane_import_admission()` establishes an async
  request-scoped context for the endpoint.
- `maintenance_import_admitted()` rejects direct service writes without that
  verified context before the Xray writer guard can create its lock file.

The verifier does not establish presentation/UI identity. The import endpoint
rejects browser-origin requests. Offline database rebuild remains blocked
until a genuine host lifecycle/quiescence issuer is qualified; see
`docs/CONTROL_PLANE_MAINTENANCE_ADMISSION.md`.
