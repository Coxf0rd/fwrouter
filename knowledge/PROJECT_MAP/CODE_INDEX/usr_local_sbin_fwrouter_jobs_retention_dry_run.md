# `/usr/local/sbin/fwrouter-jobs-retention-dry-run`

## Purpose

Systemd wrapper for the daily jobs retention dry-run diagnostic.

It posts a `jobs_retention_cleanup` job to `http://127.0.0.1:5000/api/v2/jobs` with `input_data.dry_run=true`, lock `jobs_retention`, and `requested_by=systemd_timer`.

## Review Notes

Read the source file directly before changing related behavior. Check the Jobs API route and `jobs_retention_cleanup` handler because the wrapper relies on the API guard that forbids non-dry-run cleanup through the generic Jobs API.

## Runtime Impact

This wrapper must not delete state. It is a diagnostic guard around the retention path:
- successful job status is required;
- job input must still have `dry_run=true`;
- when full `job.result.retention` is present, nonzero deleted jobs/artifact dirs are treated as errors;
- when the API truncates a large successful result to `__truncated__`, the wrapper accepts it only if job input proves `dry_run=true`.

## Guardrails

- Real cleanup is performed by the maintenance path, not this timer.
- Do not treat a truncated successful dry-run result as proof of deletion; the wrapper cannot inspect deleted counters after truncation.
- Do not accept missing retention details unless `job.result.__truncated__` is true and job input is dry-run.
