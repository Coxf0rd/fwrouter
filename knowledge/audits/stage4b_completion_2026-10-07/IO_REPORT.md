# Stage 4B I/O, Journal, WAL, and Retention Evidence

Audit slice: `journal/WAL/retention/background writes` on the deployed FWRouter host. Source baseline at start: `49591c2`. Filesystem and journal reads were passive. The initial SQLite query used `mode=ro&immutable=1`; because immutable mode can ignore a live WAL, its counts below are approximate, non-authoritative observations rather than a concurrency-consistent DB snapshot. No DB, service, or provider state was changed. A later 20-second WAL stat sample followed the main agent's API deploy/restart and is labeled separately.

## Findings

### Maintenance cadence and duplicate work

- `/opt/fwrouter-api/.env` has `FWROUTER_MAINTENANCE_SCHEDULER_ENABLED=false`; the API-thread daily maintenance scheduler is therefore disabled. The only full cleanup maintenance observed is the daily `fwrouter-maintenance.timer` invocation. This rules out two full maintenance loops running daily in the current configuration.
- The separate `fwrouter-jobs-retention-dry-run.timer` runs roughly 15–20 minutes before full cleanup. Its helper POSTs a `jobs_retention_cleanup` job with `dry_run=true`, validates that the dry-run made no deletions, and requires the job to finish successfully. This is an intentional operational canary. Its cleanup result is large because it carries candidate details: historical journal samples show 674,290–826,110 serialized result bytes before the existing job-result bound reduces the stored job result to 192 bytes. The scheduled job row and job lifecycle writes are intentional; the cleanup dry-run itself does not delete rows or artifact directories. Treat candidate-list construction/serialization as measurable cost, not as an accidental write or a confirmed defect. Preserve the canary contract.
- Daily full maintenance took 3.7–42.2 seconds of reported CPU time and 5–47 seconds wall time on Oct 1–7. Its summaries report 1,667–2,024 job rows deleted, 369–6,797 SQLite operational-log rows deleted, and `database_vacuumed=true` on each observed run. These are real retention operations, not no-op writes. A VACUUM is requested when cleanup has deleted/compacted eligible data; the observed daily runs had eligible rows.

### SQLite and WAL

- The immutable-mode read before deploy reported a 22,257,664-byte main DB, 5,435 pages at 4,096 bytes, `freelist_count=0`, 1,424 operational-log rows, and 1,510 job rows. Immutable mode can ignore WAL content; these figures are not authoritative counts and are excluded from conclusions. No claim about exact concurrent DB row counts is made from them.
- Following the API restart, three passive stat samples from 08:39:45–08:40:05 UTC showed the DB fixed at 22,355,968 bytes with no persistent `-wal` or `-shm` files. This is a short idle observation only; earlier samples overlapping restart were discarded. It does not establish WAL bytes written during normal request traffic or checkpoint cost.
- No WAL/write amplification bug is confirmed. Before/after SSD write attribution requires a longer counter window and must not be inferred from DB mtime or freelist alone.

### JSONL retention and journal I/O

- Current source already avoids creating a temporary rewrite when the first scan finds no expired JSONL lines. On the preceding maintenance cutoff, the oldest operational event was newer than its 3-day cutoff and the oldest technical entries were newer than their 14-day cutoff; the no-expired path therefore returned before opening a rewrite temp file.
- At the pre-deploy scan, the JSONL tree occupied about 15.5 MB. Current expired candidates were 15 operational lines (~569 KB) and 10 technical lines (~106 KB). When expiry exists, the implementation must scan again and atomically rewrite the affected append-only files; the rewrite is required to remove those records. This can rewrite a multi-megabyte file to discard a much smaller expired subset, but there is no evidence that the prior no-op rewrite remains.
- The next useful storage measurement is bytes rewritten per retention run and total filesystem block writes over a representative week. Current evidence does not justify changing retention format or cadence.

## Classification

| Path | Classification | Evidence / action |
|---|---|---|
| JSONL no-expiry rewrite | Existing fix; no confirmed current defect | Source early return; preceding cutoff had no expired records |
| Daily cleanup job retention | Expected work | Eligible rows deleted each observed run; no no-op cleanup proven |
| Scheduled dry-run canary | Expected, with measurable projection cost | Explicitly validates dry-run contract; preserve; result is bounded before DB storage |
| SQLite WAL/SSD write profile | Needs longer measurement | Short post-restart idle sample showed stable DB/no sidecars; insufficient for write-rate claims |
| JSONL retention rewrite with expired rows | Expected work; write-volume tail | Atomic rewrite is required; measure bytes rewritten before considering a different retention design |

## Boundaries

No tests were run for this read-only evidence slice. The main task's post-deploy verification should own final service/runtime acceptance. This report does not claim provider-request counts, Health/API acceptance, or a full SSD wear profile.
