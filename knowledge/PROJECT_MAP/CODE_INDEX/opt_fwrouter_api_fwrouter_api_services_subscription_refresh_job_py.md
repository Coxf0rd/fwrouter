# `/opt/fwrouter-api/fwrouter_api/services/subscription_refresh_job.py`

## Purpose

Tracked job handler contract for full job-backed subscription refresh.

## Important Functions

- `register_subscription_refresh_handler(manager)`
  Registers the `subscription_refresh` handler on an existing `JobManager`.
- `run_subscription_refresh_job(job)`
  Runs the existing Phase 2 subscription refresh pipeline as a background job.
- `subscription_refresh_stage(result)`
  Maps legacy pipeline internals to the long-operation stages: `download`, `parse`, `persist`, `prepare`, `validate`, `apply_runtime`, `verify`.

## Runtime/Persistent State

- Mutates subscription inventory, source memberships, generated Mihomo candidate/active config, and Mihomo runtime through the existing subscription pipeline.
- Job success is returned only after runtime verification/reconcile succeeds.
- Structured failures include `job_id`, `operation`, `stage`, `error_code`, `message`, and source context when available.

## Boot Persistence Relevance

Medium/high. The job can refresh persistent server inventory and generated Mihomo artifacts used after boot.


### Terminal outcome contract — 2026-10-01

The existing subscription job accepts an optional stable `source_ref`; no source
URL or credentials are persisted in job input. Targeted and full operations use
the same refresh/delete lock. Partial results use failed terminal status plus a
typed outcome, source outcomes, runtime verification and last-good retention.
Success requires complete verified outcome; no-op is a verified unchanged state.
