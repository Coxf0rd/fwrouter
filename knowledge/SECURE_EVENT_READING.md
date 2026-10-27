# Secure event reading

## Scope and owners

This Stage5 package adds an opt-in read capability to the existing host-owned
client registry. SQLite `operational_logs` and the existing domain event writers
remain authoritative. It adds no storage, listener, audit writer, network exposure,
retention policy, global revision, session platform or mutation authority.

The source baseline is main `be5395541b1417bb76316179e9c43673d5254dfa` (PR56).
Actual-main run40559068515 attempt1 passed all16 mandatory jobs, strict aggregate,
main smoke and independent clean-source offline bundle verification. This does
not establish installed-host state. See [logging access and retention](LOGGING_ACCESS_AND_RETENTION.md)
and [client attribution](API_CLIENT_ATTRIBUTION.md) for earlier bounded contracts.

## Access contract

The read surface is `/api/v2/events/secure/recent`: GET for local CLI/integration
requests and read-only POST for browsers. Both methods use the same bounded query
and handler and require an explicit
`events.read` registry scope, independently provisioned by the host administrator
through the existing `fwrouter api-clients create --scope events.read` workflow.
Existing credentials gain no scope automatically. Presentation mutation and
maintenance credentials do not imply this authority; read-only credentials grant
no presentation mutation or maintenance authority. Registry configuration is not
imported with control-plane data.

Admission requires the existing completed registry cutover (selector and durable
fence consistent, no pending policy publication); this package does not perform
that cutover or activate a pre-cutover registry. Scope independence is not a second
policy selector or authority owner. Missing, malformed, invalid, revoked or wrong-scope
credentials cannot fall back to an anonymous response or a legacy shared verifier.
Corrupt, missing, pending or inconsistent policy fails closed. Raw credentials,
verifier values, host paths and policy documents are never returned in errors.

The existing registry policy lease serializes bounded admitted reads against
policy updates/revocation. A page reauthenticates independently. Revocation waits
for an already admitted page to finish constructing its response; it cannot recall
bytes from a response already admitted or sent. No grant is transferred to jobs.
Successful reads and denials do not recursively emit additional audit events.
Responses must be private and non-cacheable, including failures.

Transport restrictions remain conservative: web_ui credentials require the exact
configured canonical HTTPS Origin and Host. CLI/integration credentials require
no Origin, the fixed local Host `127.0.0.1:5000` and actual loopback peer. Headers,
Origin, requested_by, roles and User-Agent are never client identity. Credentials
prove possession, not which physical program used them. There is no proxy-header
trust or external-integration activation in this package.

## Safe projection

Only a bounded allowlist may cross the protected read boundary: event ID, code,
timestamp, safe action/outcome, recorded verified client ID/kind, historical tag
snapshot and assurance, and the server-owned canonical UUID API request identifier. No raw
message, details, before/after payload, diagnostic dump, credential ID, policy
revision, verifier, token or secret is exposed. Fixed-kind labels are derived from
recorded verified metadata, never the caller or current renamed registry record.

Legacy or malformed identity remains explicitly unverified. Caller-supplied request/job/correlation strings are omitted entirely from this
projection: validating their characters does not prove they contain no secrets.
The server-issued API request UUID is the only exposed request-correlation field;
legacy/invalid snapshots never fall back to caller correlation.
HTTP200, intent_committed, application completion and native verification remain
different facts. Reading must never upgrade an event's recorded outcome.
Recorded identity is not a cryptographic forensic attestation: a trusted host/DB
administrator can alter historical data. No historical legacy attribution is
retrospectively verified.

## Bounded history and compatibility

History uses bounded keyset traversal, not OFFSET or a full-history count/filter
scan. Each request has a hard scanned-row, details-size, result-count and cursor
budget. The pagination cursor is a locator, not authority, secret, durable global
revision or proof of unchanged history. Head anchors detect deletion or a changed anchor identity/action/timestamp;
stale pagination must restart. This is not an immutable full-history snapshot or
database-incarnation proof across requests. An unchanged anchor does not prove
that every older row is unchanged. Legacy NULL/empty stored IDs are exposed as
`event_id: null` with unverified identity; their locator cannot distinguish a
same-rowid replacement with identical safe anchor fields. Malformed nonempty IDs
fail closed rather than leaking an arbitrary identifier. Concurrent newer events
are read with a fresh first page.
An empty filtered page may still have continuation when its scan budget is spent.

Ordinary `/events`, `/logs`, `/jobs`, diagnostics, summary/full/detail and UI
projections keep their existing behavior and reserved-identity redaction. They
cannot use flags, headers or supplied read credentials to upgrade their projection.
This does not retrofit global authorization or prove arbitrary legacy free-text
logs contain no secrets. No Web UI visual change or new CLI subcommand is included;
UI/CLI/integration callers use the same explicit protected projection. Browser
clients use read-only POST, not an assumption that browser GET includes Origin.

## Activation and remaining gates

Source/isolated CI acceptance does not authorize production activation. Before
issuing/using read credentials on a deployed API, separately qualify all-worker
version compatibility, canonical proxy/TLS/origin and script integrity, credential
custody/revocation, ACLs/backups, legacy-reader external reachability and bounded
query behavior at representative history sizes. Older registry validators may
reject policies containing the new scope; do not downgrade/rollback policy or
restore shared-verifier fallback to work around this.

Audit/security HOLD remains temporary; no expiry horizon changes here. The host
administrator must not activate this API by merely opening a proxy route or
reusing write credentials. No proxy, listener, service, live DB or credentials are
changed by this source package. L6/L7 and live access are separate qualifications.

## Query and response bounds

Supported query fields: `limit` (default50,1–100), `category` (`audit`/`security`),
exact `event_code`, UTC `since` (`YYYY-MM-DDTHH:MM:SSZ`), and `cursor`. Duplicate
or unknown parameters fail with a constant safe error. Total query bytes are
bounded at4096; cursor bytes at1536. A page inspects at most250 candidate rows
and at most64KiB of details per row, using read-only SQLite and a1000ms busy wait.
Payloads outside that projection budget do not become unrestricted diagnostics.
Known security codes are currently the existing owned
`client_list.mutation_denied` and `client_list.mutation_conflict`; this is not a
claim of complete security-event coverage. Unknown safe legacy audit records use
a fixed fallback code rather than reflecting arbitrary stored action strings.

Responses contain `events`, `scanned` and `next_cursor`; an empty page with a
non-null continuation is valid. Reuse the same filters with the returned cursor.
The timestamp retains existing UTC SQLite representation. A stale cursor returns
409; bad queries400; missing/invalid credentials401; wrong scope/transport403;
unavailable policy or data503. All use no-store/private responses with stable
error codes, not echoed input or internal exception strings.

## Example safe event fragments

All authorized readers receive the same projection; the identity below describes
the recorded initiator, not the program currently reading the page. Client IDs and
request IDs are omitted from these partial examples for brevity.

```json
{"event_code":"client_list.show_inactive_changed","outcome":"intent_committed","identity":{"assurance":"credential_bound","kind":"web_ui","tag_snapshot":"presentation-admin","display":"[WEB_UI]"}}
{"event_code":"client_list.show_inactive_changed","outcome":"intent_committed","identity":{"assurance":"credential_bound","kind":"cli","tag_snapshot":"cli-admin","display":"[CLI]"}}
{"event_code":"client_list.show_inactive_changed","outcome":"intent_committed","identity":{"assurance":"credential_bound","kind":"integration","tag_snapshot":"event-reader","display":"[API:event-reader]"}}
{"event_code":"user_action","outcome":"unknown","identity":{"assurance":"unverified"}}
```

These are illustrative fragments of the expected isolated HTTP-owner round-trip
contract, not production observations or proof of native application. No token is
placed in a query parameter, browser storage or a printed command line by this
contract. No browser credential activation or new CLI command is performed.

## Privacy review correction

Independent review rejected broad character validation for caller correlation: a
value such as `token:...` can contain a secret despite looking like an identifier.
The secure projection therefore omits caller claims, requested_by, principal and
job correlation of unproved provenance. Existing raw/legacy readers retain their
separate compatibility/redaction contracts; this endpoint neither grants them
new authority nor asserts arbitrary historical text is free of secrets. Async
job correlation requires separately proved producer provenance before exposure.

## Canonical stored identity only

Verified projection requires the complete current producer snapshot: canonical
`api_client_id`/`api_credential_id`, valid registered kind, explicit historical
`tag_snapshot`, positive integer policy revision, canonical server API request
UUID, and exact credential_bound/verified markers. The private credential ID and
policy revision are validation inputs only and are never returned. There is no
fallback to internal client_id/credential_id aliases or to formatted display_tag.
Missing/malformed fields produce only unverified identity, with no client IDs,
tags or server request ID. Labels are derived from kind and historical raw tag.
This matches the existing writer; it does not upgrade older ambiguous payloads.

## Browser method compatibility

The [Fetch Standard Origin algorithm](https://fetch.spec.whatwg.org/#origin-header)
does not normally add Origin to a same-origin GET/HEAD, while browser POST carries
Origin. Keeping strict canonical Origin/Host admission therefore requires a
browser-compatible read-only POST, rather than weakening admission or relying on
JavaScript to forge a forbidden header. POST uses the same query parameters,
returns the same safe DTO and creates no event/domain write. It grants no mutation
authority and accepts no credential in a body/cookie/query. This source contract
does not qualify a deployed browser, proxy, TLS or credential custody.
