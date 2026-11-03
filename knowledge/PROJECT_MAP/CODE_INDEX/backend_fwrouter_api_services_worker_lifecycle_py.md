# `backend/fwrouter_api/services/worker_lifecycle.py`

## Purpose

`WorkerLifecycle` owns the thread handle, cooperative stop Event, and lock for
one existing scheduler owner. Each scheduler constructs its own instance.
The helper consolidates repeated start/stop bookkeeping; it does not own tick
policy, enablement, first-run delay, interval, admission, logging, or workers
as a group.

## Concurrency contract

- Starting while the current worker is alive returns `False` and leaves its
  stop signal untouched.
- Stop sets the Event before joining. A timeout retains the live thread handle
  and cancellation signal; a later start is rejected until it exits.
- The lock is held through join to serialize concurrent start and stop calls.
  Worker loops access only the Event and must not call back into this owner or
  acquire its lock.
- A factory start error before the worker begins drops the unjoinable handle.
  If the worker became live before the error, its handle is retained and
  cooperative stop is requested.
- Thread name, daemon setting, public scheduler facade, and stop return values
  remain selected by each scheduler.

No worker is force-cancelled, and no additional thread is created by this
module.
