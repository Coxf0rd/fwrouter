# `/opt/fwrouter-api/fwrouter_api/services/jobs_retention.py`

## Purpose

Deletes old completed `jobs` rows and their artifact directories under `/var/lib/fwrouter-v2/jobs`.

## Important Functions

- `cleanup_jobs_retention(...)`
  Deletes completed jobs by age/count policy while protecting `queued` and `running` jobs.

## Runtime/Persistent State

- SQLite `jobs`
- `/var/lib/fwrouter-v2/jobs/<job_id>`
- `rules_state.last_apply_job_id` / `rules_state.last_update_job_id`

## Review Notes

Read the source file directly before changing related behavior. Check adjacent service, route, adapter, script, or systemd documentation as applicable.

## Runtime Impact

This file is part of the FWRouter source/runtime surface. Keep this card synchronized when the file responsibility, runtime side effects, boot relevance, or risk profile changes.

## Behavior Notes

- Retention uses both age and max-count policies per `job_type/status`.
- `apply_mutation` success/failed jobs have narrower limits because their dataplane artifacts are heavy.
- Cleanup clears `rules_state` job references before deleting job rows.
- Destructive delete path uses the shared DB connection with `PRAGMA foreign_keys=ON`, so `apply_versions.job_id -> jobs.job_id ON DELETE SET NULL` cannot leave orphan rows.
- Dry-run reports candidate artifact bytes and orphan artifact dirs.

## Guardrails

- Keep FWRouter core as the authority for classification and policy routing.
- Keep Mihomo as a VPN egress adapter, not the network policy engine.
- Preserve direct-safe behavior for host/control-plane traffic unless an explicit scoped contour says otherwise.
