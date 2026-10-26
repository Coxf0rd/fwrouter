# Control-plane restore publication and lifecycle exclusion

Status: implementation under qualification in PR 52. Historical admission-only
results do not qualify this extension. No deployment or live restore is authorized.

## Import commit boundary

The existing importer owns imported SQLite rows and `rules_state` references.
It prepares all eight rules files in a new private `state/import-rules/release-*`
directory, reads each file back, and fsyncs files and directories before activation.
Preparation never writes an active rules path. The existing SQLite replacement
transaction publishes all path references together with the imported rows and a
local operation receipt. This transaction uses connection-local SQLite
`synchronous=FULL`: ordinary runtime connection policy is unchanged. SQLite
commit is the publication point; there is no
second filesystem pointer, recovery worker or generation owner.

The importer holds the existing Xray writer guard and reserves the SQLite writer
with `BEGIN IMMEDIATE` before eligibility checks. An active job causes a conflict
before preparation. Jobs cannot transition into running through SQLite until
the import transaction ends. Existing Core and VPN-auto revisions remain local;
foreign fences and local maintenance receipts are not restored from a snapshot.

Ordinary rules writers use the existing path owner. Their row publication may
not rebind paths captured before an import: the upsert compares all eight
existing path references. Draft writes follow the current path rather than
resetting paths to bootstrap defaults. Old files remain available for readers
that captured the previous row. This does not claim a globally atomic read of
unrelated API projections or change native routing publication.

## Failure, restart and retry

| Boundary | Durable/visible outcome | Retry |
|---|---|---|
| Before or during file preparation | Previous SQLite rows and active files remain selected | Prepare a fresh complete set |
| After preparation, before SQL commit | Previous committed rows select the previous file set | Unpublished directory is not authority |
| After SQL commit, before response/projections | Complete new rows and complete prepared files selected; receipt committed with them | Return persisted receipt without advancing revisions again |
| Later legitimate mutation/import | Previous receipt is historical, not current native proof | Replaying its ID does not restore old intent |

A caller may supply a 32- or 64-character lowercase hexadecimal `operation_id`.
Absent an ID, a canonical snapshot plus normalization-mode digest identifies the
operation. Reusing an ID with different input is a conflict. A deliberately new
import of identical input requires a new ID. Local receipts are retained across
imports, never accepted from foreign snapshots, and never exported as intent.
The finite 4096-record capacity fails closed rather than evicting retry authority.
Receipt replay never repeats selector apply; pending reconciliation still belongs
to the existing fenced selector lifecycle.

Post-commit projection/log failures are reported separately as unavailable;
they cannot truthfully be described as an uncommitted import. A persisted receipt
is not applied/native verification. The UI/API must retain that distinction.

Controlled preparation errors remove only the uniquely created unpublished
partial directory. Process death may leave an unpublished directory; it is kept
for forensic inspection and is never auto-adopted or recursively swept by age.
Committed and previous directories must not be removed while their references
or readers can remain valid. No speculative rollback of SQLite is performed.

## Offline ownership

Offline exclusion belongs to the existing controlled backend lifecycle, not to
a request field or the Xray guard. Its extended protocol must hold the lifecycle
lock, publish a durable pending phase, drain supported writers, and retain an
exclusive resource lease through candidate construction, validation, publication
and the terminal checkpoint. The existing maintenance operation pins the original
backup basename and SHA256 before candidate construction. Every candidate reuse,
publication and published receipt replay requires that same protected backup;
missing or mismatched evidence is STOP, never a replacement snapshot of NEW.
An initial crash before pinning permits adoption only while still pending, with
no candidate/published receipt and the source logically equal to that backup.
API startup must be blocked before side-effectful
host preflight while an interrupted operation remains pending. Supported API,
CLI, maintenance and installer paths participate in the same lease protocol.

Candidate rebuild publication must preserve the qualified WAL backup and local
selection eligibility guards. A candidate is built privately and validated;
there is no permitted unlink/reinitialize window in the live database path.
An interrupted command must leave startup blocked until explicit owner-controlled
forward completion proves the operation's identity and published result.
Checkpoint publication uses a fresh private temporary name on each attempt;
crash leftovers are never authority and cannot reserve the next retry's name.
Timers
and dependent services are never resumed implicitly by rebuild completion.

Arbitrary root writes, manually edited units, unsupported old binaries and raw
external SQL do not honor cooperative leases. They are an explicit excluded
writer boundary, not proven safe by flock. Backup verification performs the final
raw SHA check after logical scans and immediately before publication. It does
not promise an atomic check-and-rename against an administrator mutating protected
files after that final check; such a writer is outside cooperative ownership.
Schema-upgrade offline eligibility
remains a separate contract; sharing a lease only excludes overlap with rebuild.

## Bounded rebuild compatibility floor

Snapshot v2 is not a complete database backup format. It omits provider and
subscription credential/identity tables, operational histories and other domain
state. Rebuild therefore refuses populated tables outside the transfer owner's
supported set before backup or candidate creation, rather than dropping them
when constructing a new database. Unsupported sequence high-water state also
blocks rebuild. Candidate schema objects must match the source: unsupported
triggers, views, indexes or tables cannot silently disappear in a rebuild.
This conservative admission commonly rejects an already-used
installation; it is not a general production restore feature. An ordinary
WAL-safe database backup remains complete independently of transfer eligibility.

The source Core intent high-water remains local and is carried into the candidate
before the importer advances it. Selection and native-generation eligibility
restrictions remain in place. Reconciliation completion is separate from durable
publication, and no rebuild receipt constitutes native proof. A failed or missing
inventory reconciliation cannot produce a successful lifecycle completion.

## Qualification requirements

Source qualification requires deterministic disposable SQLite/process tests at
file, SQL, receipt, lease and startup boundaries; conflicts must have no active
publication, crashes must not create false success, retries must not replay
intent, and cleanup must not remove a published dependency. It also requires
full exact-HEAD CI, native/profile and isolated recovery receipts, and independent
source/security/evidence review. Host-destructive tests and live activation are
separate NOT RUN gates unless independently authorized and actually executed.
