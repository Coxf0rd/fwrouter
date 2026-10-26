# Control-Plane Import and Rebuild Admission

## Current status

The admission-only status below is historical for HEAD `6919908`. The current
restore extension and its unqualified failure boundaries are specified in
[CONTROL_PLANE_RESTORE_CONTRACT.md](CONTROL_PLANE_RESTORE_CONTRACT.md). Do not
use the earlier CI PASS as evidence for the extended implementation.

Online snapshot import has a narrow maintenance-only API admission. Offline
database rebuild remains **blocked**: this package does not provide a genuine
host-wide lifecycle/quiescence context. Control-plane restore as a complete
workflow is therefore **not qualified**.

This is source-level admission hardening. It does not qualify import/rebuild
recovery across SQLite and filesystem publication, nor does it make import an
applied/native-verified routing operation.

## Online import verifier

`POST /api/v2/transfer/control-plane/import` requires all of the following:

- API runs with effective UID 0, as required by the fixed verifier contract.
- The ASGI request presents an IPv4 loopback peer and literal host
  `127.0.0.1:5000`.
- Exactly one `Authorization: Bearer <token>` header is present. The token is
  64 lowercase hexadecimal characters (256 random bits).
- The verifier exists at the fixed path
  `/etc/fwrouter/maintenance-import-verifier.json` and contains exactly:
  `schema_version`, `principal`, `scope`, and `token_sha256`.
- Policy values are version `1`, principal `maintenance-operator`, scope
  `control-plane.import`, and the lowercase SHA-256 digest of the bearer token.
- The verifier is a regular, root-owned file with mode `0600`, at most 4096
  bytes, under root-owned parent directories that are not group/world writable.
  The reader opens every component without following symlinks and rejects
  changed/inconsistent file identity during the bounded read.
- There is no `Origin`, `Forwarded`, `X-Forwarded-For`,
  `X-Forwarded-Host`, or `X-Forwarded-Proto` header. Content type must be
  `application/json` (optional charset is allowed).

The verifier path cannot be overridden through environment or request input;
policy is read on each request, so replacing/revoking it affects the next
request. Duplicate policy JSON keys, unexpected keys, missing policy, malformed
credentials, and ambiguous Authorization headers fail closed with a generic
403. Credentials and policy contents are not included in API errors or
operational logs.

These ASGI peer/Host checks do **not** prove that a deployed reverse proxy
cannot reach or forward the route. A local proxy can present a loopback peer,
rewrite Host, or remove forwarding headers. The bearer digest remains the
authorization proof, but proxy exclusion and the actual deployment ingress
route are **NOT VERIFIED** until the host's active proxy configuration is
reviewed.

No in-repository credential provisioning or browser capability is provided.
Provisioning the digest and securely delivering the bearer are separate host
administration responsibilities. Never place the bearer in a snapshot, URL,
browser storage, environment file, logs, or Git. Because this endpoint rejects
browser-origin requests and presentation identity is not established by this
contract, the Web UI must not call it.

Admission runs before transfer-path resolution (which can create the transfer
directory), database initialization, and the Xray writer guard (which can
create its lock file). The grant is revocable: request finalization deactivates
the shared grant object, including in copied contexts. The import owner checks
again after acquiring its writer guard and before database initialization or
import publication. A stale queued context may still create/acquire the
guard's coordination lock before that second check, but it cannot proceed to
SQLite or import files. The service owner also requires a request-scoped
internal admission context, preventing an ordinary direct service call from
becoming a second supported writer path. This is a narrow code-level boundary,
not a defense against arbitrary code execution as root or direct Python module
monkeypatching.

## Import result and partial failures

Snapshot validation occurs before database initialization. After admission,
import keeps its domain owner, existing CAS/fence checks and database
transaction. Rules artifacts are published as individual atomic files before
the SQLite transaction. Thus failures can leave one or more files changed while
the database remains unchanged; the API reports failure, but this package does
not introduce a cross-resource journal or claim automatic forward recovery.
Operators must preserve the failure evidence and must not retry blindly.

The import result reports durable `persistence_status` separately from
`selection_reconcile_status`. A completed selection reconcile does not mean
Core apply, overall runtime reconcile, or native proof succeeded. Existing
selector reconciliation behavior remains owned by its current service; pending
selection reconciliation must be handled through the supported lifecycle and
verified independently.

## Offline rebuild

`rebuild_control_plane_database()` now refuses before resolving the snapshot,
creating a backup, acquiring writer locks, initializing SQLite, or unlinking
the database. `fwrouter_api_maintenance rebuild-db` returns nonzero for this
blocked result. No caller-supplied flag, root identity, stopped-unit list, or
Xray writer lock is accepted as proof of global quiescence.

The pre-existing rebuild eligibility and WAL-safe backup logic remain covered
by isolated tests through an explicitly mocked precondition. Those tests only
regress the existing body; they do **not** qualify an offline maintenance
context or authorize the command. A future package must establish a real
host-wide exclusion/quiescence contract, including coordination with systemd,
timers, external writers and startup, before enabling rebuild.

## Deferred restore contract

No generic owner registry or managed-configuration metadata is added here. A
future domain-owned import hook must validate each owned resource before any
publication and rebase its incarnation/revision inside the existing import
transaction, while preserving local fences and import compatibility. That hook
must be designed with the eventual resource owner; it cannot be inferred or
qualified by this package.

## Qualification evidence boundaries

The isolated admission regressions exercise real request credential validation,
including synthetic non-root verifier ownership, copied-context revocation and
an admitted context waiting for the existing writer lock. Legacy rebuild-body
regressions explicitly replace the unavailable offline precondition; they do
not establish an operational maintenance lease. A rules-publication fault
regression records mixed file/database state as a failed import, never as a
successful restore.

Full mandatory PR CI additionally checks the existing WAL backup, selection
fences, native profiles, hosted/packet qualification and isolated recovery
contracts. Those results qualify the source package, not host-wide quiescence,
production ingress, live restore, or future managed-resource incarnation
handling. Preserve that distinction when reviewing CI receipts and before any
operational enablement.
