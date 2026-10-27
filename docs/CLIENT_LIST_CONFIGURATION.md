# Client-list presentation configuration

Status: source implementation under qualification. Production browser/proxy TLS,
canonical-origin forwarding, script integrity and credential custody have not
been qualified. **Activation remains blocked** until those separate conditions
are established; source/CI results do not authorize installation or deployment.

## Owner and concurrency

`show_inactive` remains a presentation setting in the existing SQLite settings
row `ui.admin_client_display.v1`. It is not routing intent, runtime discovery or
an adapter setting. The presentation owner alone changes the protected value
and `_show_inactive_contract` field metadata. Sibling writers merge their intended
deltas against the latest row and preserve that value and metadata.

A conditional write accepts a strict JSON boolean and the exact current ETag.
The field has a local incarnation and monotonic revision, not a global database
revision. A current-value no-op writes nothing; a stale precondition is rejected
even if the requested value equals the current value. Ordinary process restart
retains metadata. Legacy absent/boolean values retain their effective defaults;
explicit conditional adoption creates metadata. Malformed durable data fails
closed rather than being silently reset.

`GET /api/v2/ui/settings/client-list` is read-only and supports conditional reads.
`PATCH` requires a presentation capability and `If-Match`, accepts only
`{"show_inactive":true|false}`, and has a 4 KiB request bound. The response uses
`data.client_list` and an ETag. Existing display PUTs containing the protected
field use the same admission/precondition/owner operation; a denied mixed request
cannot partially save sibling values. An omitted protected field is preserved.

No exactly-once acknowledgement or durable idempotency ledger is introduced.
After a lost response, GET reports the observed current value/token; it cannot
prove which request caused that value. Neither UI nor CLI silently retries a
mutation with a refreshed token.

## Narrow authority

A separately provisioned protected verifier at
`/etc/fwrouter/presentation-client-list-verifier.json` identifies a
`presentation-operator` capability holder with only `ui.client-list.write`.
It is independent from the maintenance verifier and never stored in SQLite,
exported snapshots, static UI or application logs. No provisioning is performed
by normal configuration requests. Root-administrator credential custody and
revocation remain host operations requiring separate authorization.

The verifier contains schema version 1, the fixed principal, a finite scopes
list, SHA256 of a random 256-bit lowercase-hex bearer and a canonical HTTPS
origin. The API checks a bounded, protected, stable file on each admission;
missing, invalid or revoked policy denies. Caller attribution, UI admin view,
role headers, forwarded headers and loopback are not authentication.

Browser writes require the exact protected canonical Origin and Host. The
browser unlock holds the bearer in memory only and clears it on lock/navigation.
It never persists it in local/session storage, cookies or URLs. Same-origin XSS
and stolen-bearer replay remain residual risks. The local API-first CLI uses the
fixed local API endpoint and a protected credential file, bypasses environment
proxies and refuses redirects; it has no SQL or service fallback. Loopback
transport assumes a trusted local host/network boundary, not remote cleartext
API permission. Other existing API routes are not declared authenticated by
this narrow capability.

## Projection and recovery boundaries

Display-dependent read models capture one presentation snapshot. Aggregate
cache keys include the field token, preserve existing cache limits, and close a
revision fence before returning results to leaders, hits or coalesced followers.
A concurrent change causes a bounded retry; continued drift is a retryable
`CONFIG_READ_MODEL_STALE`, never old counts labeled as current. This is not an
atomic snapshot of all live observations.

Export retains ordinary setting values and strips local authority metadata.
Maintenance import validates presentation payload before file preparation,
then rebases the local incarnation within the existing import transaction.
Foreign metadata is never restored as authority. A genuinely new import
operation rebases even an equal value; replay of the same #52 operation receipt
performs no additional write or rebase. Eligible offline rebuild uses that owner
hook after the existing lifecycle-held exclusion, WAL-safe backup, selection
and native-generation guards. The presentation capability grants no import,
rebuild, routing or native operation. Arbitrary root SQL/file replacement and
old binaries bypassing the owner are outside the supported writer contract.

The #52 conservative snapshot-v2 restore compatibility floor remains in force.
This slice does not make a populated production database eligible for rebuild,
claim host/systemd restore qualification, or authorize L7/destructive tests.

## Qualification

Source tests must cover strict parsing, capability isolation/revocation,
conditional writes, all shared-row writers, import/replay/rebase, cache races,
HTTP/CLI and browser behavior. Hosted evidence must distinguish actual HTTP,
Chromium and native readback from synthetic unit fixtures. The test TLS origin
and test verifier establish isolated behavior only; they do not qualify the
deployed reverse proxy, TLS certificate trust, origin or credential custody.
