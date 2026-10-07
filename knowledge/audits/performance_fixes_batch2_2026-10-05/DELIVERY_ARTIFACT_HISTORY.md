# Delivery artifact chronology

The October 5 acceptance was interrupted after installer completion and the API restart at 16:45:59 +07 (reported new PID1720736). Immediate Health and router-summary returned200. Final postrestart parity/counter/UI captures were not completed in that turn.

`UI_BEFORE.json` is the predeploy UI flow. `UI_BEFORE_PROVIDER_METRICS.json` (09:41:03 UTC, PID1686088) and `UI_BEFORE_FLOW_AFTER_PROVIDER_METRICS.json` (09:41:24 UTC, same PID) bracket that predeploy flow. The latter was originally named `UI_AFTER_PROVIDER_METRICS.json`; it is renamed to prevent interpreting it as postrestart evidence. Original timestamp/counter content is preserved.

The uncommitted `PREDEPLOY.json` is the later refreshed preflight with all-row jobs/apply status checks and the protected target/baseline backup. The earlier committed version is retained in Git history at03a64a1. Acceptance resumed on October7 against the already-deployed source; no new deploy/restart is performed. October7 observations cannot establish continuity throughout the unobserved October5–7 interval.
