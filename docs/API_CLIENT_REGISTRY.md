# Host-owned API client registry

Status: source package under qualification. **Production activation is BLOCKED**
pending separate proxy/TLS/origin/script-integrity, credential-custody and client
migration qualification. Installing backend source does not authorize cutover.
This package does not add audit attribution fields, tags to user-visible logs,
request logging, retention changes, a web integration manager or a new service.

## Ownership and authority

The root administrator owns client registrations, credential issuance and policy
publication. The registry is protected host policy, not SQLite configuration;
control-plane export/import/rebuild must not overwrite it. The existing
presentation domain owns `show_inactive`, strict boolean validation, ETag/CAS and
its SQLite commit. Maintenance admission and lifecycle-held offline authority
remain independent. A registry credential cannot authorize control-plane import,
rebuild, routing, apply, refresh or credential administration through the API.

A registration has a stable opaque management `client_id`, mutable display tag,
kind (`web_ui`, `cli`, `integration`), status and explicitly scoped credentials.
Each credential has its own random public `credential_id`, active/revoked state
and SHA256 verifier of a random 256-bit bearer. No client or credential ID is a
VPN subject/client identity. IDs and verifier digests are not bearer values;
ordinary administrative listing must omit secrets and verifier digests. Policy bounds
are 128 clients, at most two active credentials per client, at most 128 total
credential records per client (including revoked tombstones), and 128 KiB total.
Exhaustion fails closed; this package has no tombstone compaction.

The supported scopes are `ui.client-list.write` and `events.read`. Tags, requested_by,
User-Agent, Origin and caller headers cannot establish identity or grant scopes.
An internal immutable admission identity describes the credential registration,
not an authenticated human or proof of a particular executable. Sharing a
credential attributes every holder to the same registration; this limitation
cannot be solved with client-supplied headers.

## Issuance, delivery and recovery

Use the bounded local root administrative entrypoint, not direct SQLite edits or
online enrollment. Operations cover initialization, pre-cutover origin
preparation, validate, create/list, rename, rotate, client/credential
revocation and controlled presentation cutover. Mutations use an expected
policy revision and exclusive policy lease. A stale revision or busy lease
fails rather than silently replacing another update.

`init` prepares an empty registry before cutover. It accepts either an absent
selector or a valid legacy schema1 selector with the same canonical origin;
it refuses a pending publication, permanent fence, registry selector or any
existing registry. With no selector, root `list`, `validate`, and credential
provisioning can prepare policy under the existing lease. This is administrator
access only: API admission continues to reject a missing selector. Fresh
provisioning remains closed to HTTP until cutover.
Bootstrap `list` and `validate` output marks this state as
`mode: unselected` and `admission_ready: false`. `validate` confirms the stored
policy structure and trusted file state; it does not claim that HTTP
authentication is enabled. Existing selected legacy/registry output shapes
remain unchanged.
When a legacy selector and registry disagree on canonical origin, `init`,
listing and credential provisioning fail closed; origin correction requires a
separately qualified compatibility transition. The CLI does not rewrite the
legacy selector to make the registry fit.

`set-origin` is available only while both selector and permanent fence are
absent and no publication is pending. It requires the current expected policy
revision. A changed origin advances the revision exactly once and preserves all
client and credential records; an identical origin is a no-op. A stale retry
conflicts and must be resolved by reading current root-admin policy and
comparing it with the approved target. Origin input must already be canonical
HTTPS: lowercase DNS labels or canonical IP literal, no path/query/userinfo,
default port 443 omitted, and no implicit normalization or DNS lookup.
Canonical IPv6 literals and explicit non-default ports are supported.

Once a legacy selector, registry selector or fence exists, origin changes are
refused. The CLI never removes/recreates the fence, downgrades a selector, or
resets a policy revision. A fence without a registry selector, or a registry
selector without its fence, remains fail-closed. Restoring/deleting all trusted
host policy files through a compromised root is outside this trust boundary.

Web UI and API-first CLI require distinct issued credentials before cutover.
Integrations have separate registrations and minimal explicit scopes. New secret
material must be delivered only to an explicitly selected protected no-clobber
output, never normal command stdout, logs or examples committed to Git. Failure
to deliver a credential must not report successful usable provisioning. After a
crash, check redacted registry state and protected output; do not blindly repeat
issuance or overwrite the previous output.

A rename preserves client/credential identity. Rotation creates a new credential
ID with explicit bounded overlap; revoking the old credential closes overlap.
Revoked identities are not reused. Administrative recovery uses the qualified
root tool to issue a replacement and revoke a lost credential; it does not
restore an old registry or alter SQLite. If policy or cutover state is corrupt
or ambiguous, writes remain STOP until controlled forward completion is proven.
Root compromise or malicious restoration of the complete host security state is
outside this root-controlled trust boundary.

The normal `fwrouter config` client remains API-first and uses its protected
credential file. Browser unlock remains explicit and memory-only. No credential
is embedded in static UI, URLs, browser storage, environment defaults, exports
or API responses. Browser identity is constrained by configured canonical
Origin/Host; CLI/integration use the qualified fixed local transport. This does
not authorize a new public listener or externally expose existing unauthenticated
legacy API routes.

## Secure publication and cutover

Registry, selector, permanent fence, pending publication and stable policy lock are protected fixed
host paths under `/etc/fwrouter`. Files are root-owned private regular files;
trusted path components and file identity are verified through no-follow file
descriptors. Policy publication uses private temporary files, file fsync, atomic
replacement and parent fsync. A protected durable pending intent precedes active
registry/selector replacement. Admission rejects any pending or malformed intent;
only explicit root-controlled forward completion can resolve it. Recovery checks
the recorded old/new identities and prepared bytes under the exclusive lease;
unexpected substitutions remain STOP. Registry/selector/fence are ordered publications,
not one cross-file transaction. Post-publication durability errors are honest
failures/INCOMPLETE; they are not proof that the old policy is still active.

Before cutover, the existing fixed presentation verifier schema1 remains the
only shared legacy authority and is not claimed to identify UI versus CLI.
Cutover can migrate that verifier, or activate a prepared registry from an
absent-selector bootstrap. It requires independently issued UI and CLI
credentials. It durably publishes the permanent `api-clients.required` fence
BEFORE publishing the strict schema2 registry selector. Any pending intent is
rejected before a fence write; an interrupted fence-first cutover resumes only
when the current policy bytes match the fence's prepared digest. Readers reject:

- A fence together with a legacy, missing or invalid selector.
- A registry selector without a valid permanent fence.
- A missing, corrupt, substituted or untrusted registry in registry mode.

A root operator must never remove/replace the stable lock or fence while workers
are running. Supported policy updates use the qualified tool and lease.

A target rename followed by failed directory fsync leaves admission STOP on the
pending intent even if the replacement is already visible. Resolve the exact
prepared intent using the administrative recovery operation; never infer that
an error means the previous policy is still active.

A visible selector rename whose parent fsync fails can revert after restart;
the previously durable fence still prevents acceptance of the old shared token.
There is no registry-presence fallback and no supported automatic downgrade or
policy rollback. An interrupted cutover after fence creation requires validated
forward completion. Application database restore does not remove the fence.
All supported workers must understand this protocol before activation; an older
backend downgrade is not qualified by this package.

## Revocation and concurrency

Presentation admission holds a shared stable policy lease across verification
and the existing domain owner operation. The root updater holds an exclusive
lease. Policy lease precedes SQLite/domain locks; the updater does not acquire a
domain transaction. Revocation publishes only after admitted operations drain;
it stops later admissions, not already committed changes. Bounded contention
returns a truthful retryable error. The request grant remains mutable/revocable
on exit, so copied contexts cannot become reusable authority.

No new asynchronous configuration operation or durable authorization ledger is
added. Future queued operations must separately qualify their authorization and
domain commit/recovery boundary; credential identity alone is not such a grant.

## Qualification and exclusions

Required isolated evidence includes real ASGI/API-first client behavior, separate
identity/scope checks, forged headers, credential sharing, wrong scope, stale
policy revisions, output custody, symlink/hardlink/writable-path attacks,
concurrent updates/revocation, publication errors, restart/cutover fences and
existing presentation/maintenance regressions. Mocked IO fault injection is
identified as such; it does not establish real power-loss persistence.

No production invocation, registry provisioning, cutover, deployment, rebuild,
service restart or host/network mutation is authorized by this source package.
Full event attribution, all-reader privacy projections, audit transaction
integration and retention belong to a later separately authorized package.

## Administrative command surface

The following grammar is for separately authorized host-policy preparation, not
an instruction to activate this package on production. `REV`, `CLIENT_ID`,
`CREDENTIAL_ID` and `PRIVATE_NEW_FILE` stand for reviewed values/output paths.

```text
fwrouter api-clients init --canonical-origin https://router.example.test
fwrouter api-clients set-origin --expected-revision REV \
  --canonical-origin https://router.example.test:8443
fwrouter api-clients list
fwrouter api-clients validate
fwrouter api-clients create --expected-revision REV --kind web_ui --tag web \
  --scope ui.client-list.write --secret-file PRIVATE_NEW_FILE
fwrouter api-clients create --expected-revision REV --kind cli --tag local-cli \
  --scope ui.client-list.write --secret-file PRIVATE_NEW_FILE
fwrouter api-clients create --expected-revision REV --kind integration --tag homeassistant \
  --scope ui.client-list.write --secret-file PRIVATE_NEW_FILE
fwrouter api-clients rename CLIENT_ID --expected-revision REV --tag my_bot
fwrouter api-clients rotate CLIENT_ID --expected-revision REV --secret-file PRIVATE_NEW_FILE
fwrouter api-clients revoke-credential CLIENT_ID CREDENTIAL_ID --expected-revision REV
fwrouter api-clients revoke CLIENT_ID --expected-revision REV
fwrouter api-clients cutover --expected-revision REV
fwrouter api-clients recover --expected-revision REV
```

Use the revision returned by the immediately preceding validated/listed policy;
do not hard-code revision increments or retry stale writes blindly. `recover`
uses the prepared target policy revision recorded in the pending intent (the
new revision for a registry update, unchanged revision for selector cutover).
It only forward-completes that prepared publication, never arbitrary rollback.
Each secret
output is a different new private file. `cutover` is an explicitly authorized
operation only after custody, worker compatibility and client migration have
been qualified. There is no `rollback-to-legacy` command. This package does not
add online creation/renaming/revocation or show these identities in event logs.

## Secure event read scope

`events.read` is an explicit scope in the same bounded root-controlled registry.
Existing credentials are not promoted; creation still requires an explicit
scope list and protected secret destination.
It grants only the [bounded secure event projection](../knowledge/SECURE_EVENT_READING.md).
It grants no presentation mutation, maintenance or other API authority. Existing
presentation/maintenance capabilities do not grant event read access.

Use only after completed registry cutover and separately qualified all-worker,
transport, proxy/TLS/origin, custody and access prerequisites. Older workers may
reject a policy containing this new scope. Do not roll back host policy or revive
shared credentials to bypass that failure. No source test activates the deployed
registry, opens a listener/proxy, or issues production credentials.
