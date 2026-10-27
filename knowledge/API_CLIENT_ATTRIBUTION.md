# API client attribution integration

## Scope and trust boundary

This Stage 5 package extends the existing event owners. It does not add a request
log, authentication platform, storage backend, authorization scope or retention
policy. The root-controlled client registry remains independent of maintenance
admission. A credential proves possession, not the physical identity of a program.

The adopted client-list presentation operation uses verified registry identity.
Other management endpoints without registry admission remain unverified; caller
`requested_by`, `source_type`, Origin, User-Agent and proxy headers never establish
identity. Legacy capability holders have no individual client identity. Historical
events are not rewritten or retrospectively attributed.

## Request, intent and asynchronous context

A server-generated `api_request_id` is distinct from the compatible, untrusted
`X-Request-ID` correlation. Verified metadata includes stable client/credential
IDs, client kind, event-time tag snapshot and policy revision. In stored event details the snapshot is under `api_identity`; public identifier
keys are `api_client_id` and `api_credential_id`, keeping them distinct from
business objects and credential secrets. Internal request/job snapshots use the
registry's `client_id`/`credential_id` names. No credential secret, secret digest,
Authorization header or request body belongs in attribution.

The client-list owner writes its required intent audit in the same SQLite
transaction as the setting and field revision. Audit insertion failure prevents
the mutation. A committed row proves durable intent, not response delivery,
network apply or native verification. An unchanged retry is distinct from a new
commit; If-Match/CAS and authorization revocation retain their original owners.

Existing jobs carry an explicit context snapshot, never a request grant. Job
acceptance, terminal results and native evidence remain distinct. This package
provides propagation hooks; it does not claim that unauthenticated legacy job
submission becomes authenticated or that every legacy operation emits an atomic
audit. Unknown response delivery does not authorize replay without current CAS.

## Safe readers and display

The existing log/event APIs have no qualified sensitive-read authorization.
New identity metadata is therefore withheld by default from all HTTP projections,
including full/by-ID, legacy operational/technical, diagnostics and job context.
Host-administrator inspection uses the existing SQLite/JSONL evidence; this is
not a new remotely accessible full-identity endpoint. API service `UMask=0077`
protects newly created files, but does not repair pre-existing files, directory
ACLs, backups or other writers. Actual filesystem access must be separately
qualified before activation; this package does not claim it was inspected live.

Display labels are derived only from verified metadata: `[WEB_UI]`, `[CLI]`,
`[API:homeassistant]`. Tags are event-time snapshots, not current registry lookups;
renaming cannot rewrite historical evidence or impersonate another client kind.
Unverified/legacy attribution must remain explicitly distinguishable. These labels
are not added to public log messages or the current Web UI in this package.

## Outcomes and limits

Authorization rejection, validation failure and CAS conflict describe failure
stages, not successful intent/application. Denial aggregation is bounded and must
not retain credentials or merge clients into a forged identity. Ordinary successful
GET/polling does not create additional audit rows. Failure to persist a denial is
not proof of complete audit delivery and cannot authorize the denied mutation.

Production activation remains BLOCKED pending separate proxy/TLS/origin/script
integrity, credential custody, all-worker compatibility and safe log-access
qualification. Retention changes, log UI redesign, host-admin policy lifecycle
attribution and adoption by other domain mutations are separate work. Source/CI
qualification does not establish deployment, production identity or L7 parity.

## Logging access and retention qualification

See [the access/retention contract](LOGGING_ACCESS_AND_RETENTION.md) and
[the Stage5 audit](audits/stage5_logging_access_retention_2026-10-27/REPORT.md).
Extended identity remains host-admin-only; no new remote read grant is implied.
