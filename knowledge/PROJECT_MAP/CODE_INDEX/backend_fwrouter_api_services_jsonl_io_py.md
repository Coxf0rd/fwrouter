# `backend/fwrouter_api/services/jsonl_io.py`

Shared I/O coordination for existing operational/technical/diagnostic JSONL owners.
Stable per-file Linux flock lease binds cooperating appenders, retention and
startup scrub to the same lock inode across atomic replacement. Opens/private
publication use protected directory FDs; acquisition failure has no unlocked
fallback. This is not a new event store or per-event durable forensic ledger.

Callers: `services/logs.py`, `services/events.py`, `services/logs_retention.py`,
`services/event_contract.py`. Existing/mixed-version external writers and live
file-size/lock-latency remain activation gates. See
[access and retention](../../LOGGING_ACCESS_AND_RETENTION.md).
