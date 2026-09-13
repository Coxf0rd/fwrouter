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
