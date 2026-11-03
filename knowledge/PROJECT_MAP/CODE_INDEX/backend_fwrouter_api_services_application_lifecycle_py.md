# `backend/fwrouter_api/services/application_lifecycle.py`

## Purpose

Coordinates the existing single native API process's startup/shutdown ordering
through an immutable set of narrow collaborators. `main.py` creates the
production coordinator at lifespan entry, after entering the fixed host
maintenance lease. Tests may supply a factory, but injected callbacks do not
create independent route, database, settings, cache, worker, or job universes.

## Invariants

- Full schema/integrity admission succeeds before log scrubbing, handler
  registration, scheduler start, or read-model prewarm.
- Startup keeps the existing order of bootstrap, scrubbing, extended handler
  registration, seven independent schedulers, and optional prewarm.
- Shutdown first signals every scheduler, then spends one shared 14-second
  budget across scheduler joins, prewarm, and the same JobManager instance.
- Disabled startup still drains prewarm and jobs created by request operations.
- The fixed maintenance lease and its host state root remain owned by
  `main.py`, outside injectable operations; the lease covers shutdown.

This is a lifecycle coordinator, not a service registry, dependency-injection
framework, or multi-state-root runtime.
