# `backend/fwrouter_api/services/xray_generation_fingerprint.py`

## Responsibility

Ordered SQLite read projections and exact hash encodings for ordinary generation,
proof-only admission, Core runtime observations and identity reference fences.
The ordinary and proof-only source domains remain distinct. Sensitive rows are
hash inputs, not diagnostic output.

## Connection and dependency contracts

Read functions consume an explicit caller-owned connection. They do not open a
database/session, commit, rollback, initialize a schema or change WAL policy.
The coordinator retains its existing optional-connection entrypoints and session
boundaries. The proof-only extension receives the generation fingerprint
callable explicitly, preserving the existing coordinator patch seam without a
reverse dependency or generic dependency container.

SELECT order, columns, row ordering, JSON options and error behavior are
compatibility contracts. Neither this module nor a hash grants permission to
publish, compensate identity intent or recover a checkpoint. All such decisions
remain with the existing coordinator and its transaction/fence checks.

## Verification

Golden fingerprint/SQL-projection tests and generation, identity and native
proof regression suites cover the source fence. The existing Mihomo source-file
fingerprint also covers extracted executable bytes; moving code must not remove
it from freshness evidence. No performance improvement is claimed.
