# Durable terminal job outcomes

## Owner and transaction

Jobs remain owned by `services/jobs.py` and the existing `JobManager`. Terminal
execution evidence is a typed `job.terminal_outcome` audit event in the existing
SQLite `operational_logs` event owner. It is not a second job database, a new
ledger, a native receipt or an assertion about installed/live state.

A successful guarded terminal transition and its safe event commit in the same
SQLite transaction. If event insertion or transaction commit fails, the caller
must not receive a successful terminal publication. The job and event commit
together or neither commits. An ambiguous/lost response is resolved by reading
the existing durable job/event; retries cannot overwrite the first terminal
status or emit another terminal event. Existing source/runtime transactions are
not made atomic by this contract.

A deterministic UUID derived from the server-created job UUID identifies the
snapshot using the existing event primary key. This permits bounded direct
lookup during retention without scanning all retained event JSON. Other event
writers retain their ordinary event-ID behavior. A conflicting or malformed
snapshot is an error, never permission to delete the only remaining job result.

## Evidence meaning and safe fields

The snapshot preserves the canonical job ID, terminal execution status, finish
time, a finite execution classification, and snapshot assurance. It contains no
job input/result object, arbitrary error text, credential, artifact path, runtime
configuration or raw diagnostic payload. A success is **job execution success**;
it does not establish domain apply, native verification or external connectivity.
A stale-running failure may follow actual native work whose final result was
never durably published. That remains uncertain; it must not be recast as a
proved runtime rollback or failure.

Verified identity and correlation for a new terminal event come from the job's
persisted server-origin context. They do not come from the thread/request that
happens to finish or clean up the job, supplied role/source headers, or job input.
The original tag snapshot remains historical. A credential proves presentation,
not the physical identity of a browser, CLI binary or integration process.
The event context must be isolated while reconstructing the originating job,
then the prior ambient context restored on success and failure.

`snapshot_assurance=terminal_commit` means the new terminal state and snapshot
were committed together. `retention_observation` is explicitly weaker: a
pre-package terminal row was observed before deletion. It does not retroactively
prove when the old result committed, its original attribution or native effects.
Observation time is the event time; recorded finish time remains the row's
historical value. Unverifiable legacy rows are held/fail honestly rather than
being converted into invented original-commit evidence. Jobs already deleted before
this contract cannot be reconstructed from an old request/intent event; their
missing final outcomes remain unknown.

## Retention and compatibility

The existing jobs/artifact retention owner remains unchanged. Before deleting a
terminal job it revalidates the actual row in the deletion transaction, checks a
matching existing snapshot or publishes the explicit legacy observation through
the same event owner, then deletes the job. Snapshot/row inconsistencies and
publication failure prevent that deletion. Queued/running protection and existing
status/type caps remain. Filesystem artifact deletion follows database commit;
there is no promised atomic transaction spanning SQLite and filesystem deletion.
A missing job/artifact is not required to read the retained terminal event.

Typed audit/security HOLD also protects the terminal snapshot. This is temporary
forensic safety, not approval for unlimited storage or a numeric expiry horizon.
Ordinary operational and technical defaults are preserved. Choosing separate
audit/security horizons requires measured class-specific ingress/deletion,
incident/privacy requirements, an owner-assigned disk budget and reserved room
for WAL, backup, rewrites, staging and recovery. Do not evict native/recovery
proof or held evidence merely because disk pressure appears.

Existing job HTTP DTOs and legacy read projections remain compatible. Reserved
verified attribution stays redacted on legacy readers. Only the registry-scoped
`events.read` projection may return the allowlisted terminal snapshot and validated
job/correlation identity; it must not reconstruct a result from a deleted job or
expose arbitrary stored details. Ordinary reads do not create recursive audit
traffic. This package does not alter registry admission/cutover, external proxy
reachability, retention durations or user interface design.

## Qualification and activation

Isolated tests cover transactional rollback, retry/concurrency, restart/stale
cleanup, retained results after job/artifact deletion, safe legacy observation,
context/identity isolation, hostile payload exclusion and all reader boundaries.
Exact-HEAD mandatory CI and offline source/evidence verification are separate
from deployment. Existing SQLite WAL and `synchronous=NORMAL` remain unchanged;
owned-process SIGKILL tests prove process-crash transaction behavior, not an
unconditional guarantee against OS crash, hardware/cache failure or power loss.
Destructive host SIGKILL/reboot/corruption acceptance remains
NOT RUN without a safe qualified environment and is not to be performed on
production solely to prove this contract.

Real installed worker/unit compatibility, SQLite/backup readiness, credential
custody/cutover, proxy/origin compatibility, actual native/API and external-client
acceptance belong to separately approved controlled production rollout. No source
or CI result authorizes that operation. See also
[logging retention](LOGGING_ACCESS_AND_RETENTION.md),
[secure event reading](SECURE_EVENT_READING.md), and
[client attribution](API_CLIENT_ATTRIBUTION.md).
