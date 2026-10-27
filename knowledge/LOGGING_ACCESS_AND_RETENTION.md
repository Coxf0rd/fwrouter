# Logging access and retention qualification

## Existing owners and access boundary

The event owner remains `services/events.py`, with SQLite `operational_logs` as
canonical typed audit/operational storage. Compatibility operational writes also
append an operational JSONL mirror; typed transactional audit writes do not.
Technical/diagnostic writers use component JSONL. Jobs retain their existing
SQLite record/artifact owner. None is a new tamper-evident or exactly-once ledger.

Existing ordinary API/UI readers are compatibility projections, not an
administrative authentication boundary. `/events/recent` (including `view=full`),
`/events/{event_id}`, `/logs/operational`, `/logs/technical` (including
`ui_only=false`), `/jobs` and job detail, `/rules/jobs/{id}`, diagnostic/history
and UI workspace/router-summary projections have no uniform read authorization.
The `fwrouter diagnose` CLI is API-backed; loopback transport does not authenticate
its caller. UI admin view, headers, requested_by, Origin and User-Agent confer no
read authority. Reverse-proxy reachability and filesystem permissions are NOT
qualified by a source inspection.

PR55-compatible HTTP projections continue to strip reserved `api_identity`,
`_api_identity`, `api_request_id`, and `verified_api_*` fields recursively. They do
not expose the newly stored client/credential IDs, tag snapshot or policy revision.
This is not a claim that arbitrary legacy diagnostics are nonsensitive. Legacy
actor/source/correlation fields remain caller assertions unless their producer
independently establishes otherwise. Existing credentials/redaction patterns are
preserved; unknown secrets in free text, third-party logs, subscription path
segments, raw traces and proxy/system journals require separate classification.
There is no generic log-file download route; control-plane transfer excludes logs,
jobs and host registry policy. Backups and direct SQL/JSONL remain host-admin data.

## Minimal extended-attribution access contract

For this package, extended attribution remains local host-administrator-only in
existing SQL/JSONL evidence. No HTTP flag, role/header, component filter or detail
view upgrades a reader. Presentation write and maintenance capabilities are
independent and do not grant sensitive log reading. No new listener, credential,
scope or registry cutover is introduced.

The preferred future remote boundary is an independently provisioned least-
privilege `events.read` credential using the existing registry verifier/lease and
transport constraints, with an explicit bounded read projection. Registry support,
route admission and client custody must be qualified together before enabling it;
that scope is NOT implemented by this package. Verify an invalid supplied
credential as a denial on that protected surface, never anonymous fallback.
Existing public compatibility readers remain redacted and do not claim to
validate supplied credentials. The extended projection must separate verified
client identity from caller claims and preserve event-time tag snapshots. It may
include stable public IDs, kind, tag, assurance and event/request/job correlation;
never verifier digests, tokens, cookies, raw headers or configuration secrets.
Reading ordinary pages must not recursively create an audit storm. Root-level
filesystem authority is not proof of an individual human principal.

## Retention safety contract

The generic operational retention window must not silently delete typed audit
or known security denial/conflict records merely because they share a table/file.
The bounded source fix places these records on an explicit temporary HOLD while
ordinary operational cleanup keeps its existing three-day policy. Existing
technical diagnostics retain their fourteen-day policy; this is inherited
behavior, not a newly qualified fourteen-day audit/security term. Malformed or
unclassifiable records are retained for administrator investigation.

HOLD is a safety interlock, not an approved infinite-retention production policy.
No new audit/security deletion horizon is selected without a supported recovery
window, incident detection/response horizon, data-minimization requirement and
measured production volume/disk budget. The previous design's fourteen-day
example was not an empirical workload measurement. Required next decision is a
separate explicit horizon/budget for audit and security, not extending all logs
to the same age. Do not silently delete forensic evidence on disk pressure or
claim that old purged events can be reconstructed. Security here means the fixed
owned denial/conflict codes; it does not assert complete security-event coverage.

Job retention remains independently owned: success/cancelled normally one day,
failed fourteen days, with stricter per-type counts and queued/running protection.
An old audit's job ID may therefore outlive its job/result/artifacts. Before
claiming a complete long-term request-to-final-result trail, qualify a terminal
outcome snapshot and its retention in the existing event owner; changing jobs or
native/recovery retention is outside this package.

JSONL appenders, retention and startup redaction must cooperate through the same
stable per-file lease. Atomic rename alone does not preserve an append made after
a rewrite reaches EOF. Lock acquisition failure must fail honestly; no unlocked
fallback. Private replacement and cleanup must be tied to owned paths/inodes and
must preserve the previous published file on pre-publication failure. Directory
fsync failure after rename has uncertain durability and is not rollback proof.
A log append is not thereby an fsync-per-event durable forensic transaction.
Linux procfs must expose the current process FD directory for anchored opens;
absence fails closed with no absolute-path fallback. New or rewritten logs are
private0600; previously broader file modes are tightened on append. Log roots
with symlink, untrusted-owner or unsafe writable ancestors are rejected. Check
that compatibility floor before rollout rather than silently relocating files.
Older writers/external appenders not participating in this lease remain an
activation blocker; all participating API/maintenance workers must be upgraded.

## Measurement and production gates

The accompanying audit measures real event-writer output in isolated synthetic
SQLite/JSONL states. Generation throughput is NOT the observed production event
arrival rate. Production logs, database, proxy, services and policy were not read
or changed. Current deployed access, UID/GID/ACLs, backup custody, journald/proxy
rotation, workload/retention capacity and all-worker compatibility remain UNKNOWN.

Before production activation: qualify proxy/TLS/origin and script integrity;
credential issuance/custody/revocation; legacy route external reachability;
log/DB/backups ownership and permissions; cooperating worker versions and lock
latency at measured file sizes; production arrival/byte growth with a separately
authorized aggregate-only observation; audit/security expiry and disk budgets;
and retention of terminal evidence. No deployment or automatic cleanup of live
records is authorized by source/CI qualification.

## Subsequent Stage5 secure-reading package

The earlier host-admin-only restriction above describes the pre-read-capability
checkpoint. The separately authorized [secure event reading contract](SECURE_EVENT_READING.md)
adds only an explicit registry `events.read` boundary and safe bounded projection.
It does not expose reserved identity in any of these legacy projections, grant
read authority to existing credentials, change retention, or qualify deployment.
Production activation and external reachability remain separate gates.
