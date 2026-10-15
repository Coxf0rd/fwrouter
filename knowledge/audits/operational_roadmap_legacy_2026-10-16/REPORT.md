# Operational roadmap synchronization and legacy audit

## 1. Production and Package 1 acceptance

**Package1 ACCEPTED in confirmed scope**: PR13 squash3f96e1e, actual full mainCI [38804147992](https://github.com/Coxf0rd/fwrouter/actions/runs/38804147992)1660 PASS/0 FAIL/0 SKIP; deployed backend/docs442 files, source verified,27 protected host files and78 original Xray UUID/email pairs retained. API/Gateway/workers/timers active; routing/VPN/Xray in_sync, native containers not restarted, observed fail-closed/DNS/Rescue/Tailscale preserved. [Deployment handoff](../operational_package1_production_2026-10-16/FINAL_HANDOFF.md) is the unchanged historical acceptance receipt; [source report](../operational_package1_2026-10-16/REPORT.md) contains synthetic benchmarks and their limits.

Synthetic configured-read196→45.7ms and34.45MB→1.5KB, sanitizerCPU−60–87% are component results, not whole-production savings. Deployment window460.034s:47/47 healthy,6/6 bounded parallel reads,CPU27.589% one core,RSS673.54→727.16MiB,max917.91MiB,HWM1045.53MiB,OOMdelta0,health p50/p95/max5.098/272.4/718.239ms,startup26.104s. Planned ten-minute observation was stopped at user request, not silently completed.

Fresh read-only follow-up at API age4816s (~80min): samePID1772364,6/6 health,442 source files/27 host files and78 originals reverified,OOM counters0. RSS≈1251.7–1339.0MiB,HWM1485.4MiB,cgroup current1623875584B. [Sanitized follow-up](LIVE_FOLLOWUP_SUMMARY.json). This is two separated observation windows, not a continuous hour-long soak. Retained-memory growth is a real risk observation with UNKNOWN cause (cache/allocator/workload/leak not separated), not established Package1 regression or leak. No critical failure proven; retain long-term gate.

Synthetic Functional PASS; Production State PASS in observed bounded scope. External Client E2E NOT RUN; full Debian Host/reboot/rollback NOT RUN; full release/overall Production Acceptance remains BLOCKED on separate applicable gates. Historical user-confirmed external Rescue is preserved; no fresh external handshake claim. No real upstream probes, subscription refresh, credential mutation, deployment or service restart in this task.

## 2. Roadmap reconciliation and chronology

Canonical contains newer rollout/re-audit/Package1 receipts than Git mirror. Exact pre-edit canonical/mirror/register/project-map content is archived under [history](../../history/operational_reconciliation_2026-10-16/CANONICAL_BEFORE.md), not discarded. Active functional plan is derived from canonical, not rewritten from an older Git checkpoint. Superseded PAUSED/PR-open/source-only notices move intact into history; active status tables are reconciled. All unrelated Stage5→DB→6/6A→8→7→Post-Extraction→SDK/metrics/UI/documentation→9→10 order and security/config/DNS/orchestration/release decisions remain.

Four pending patches were inventoried with SHA256 before edits: CI postmerge sync, prior rollout sync, resource re-audit proposal, Package1 rollout proposal. No patch was blindly applied. New factual content is integrated into current sections; historical source paths remain archived. Old patches remain byte-identical, superseded as publication proposals by this reviewed PR. No raw DB/backup/config/journal/vendor files enter Git. Canonical active publication is guarded by its pre-edit hash after final PR qualification; Git mirror and canonical must then be identical. Existing old reports/snapshots are not rewritten.

## 3. Current package sequence and closure

| Package | Status / priority | Entry | Exit / evidence |
|---|---|---|---|
|1 CPU/configured reads/sanitizer|ACCEPTED bounded,P1|PR13/mainCI/deployment/live receipts above|Equivalent parsing/redaction, native safety, source and identity preservation; no long-term/wholeCPU claim |
|2 Cache Lifecycle|PLANNED,P1; not authorized|Matched synthetic baseline; owner/key/revision/incarnation/TTL/value-byte inventory|Bound expired references, entries/bytes and eviction work; concurrent invalidation/error/restart parity, independent modules/connections; CPU/RAM before/after, full requiredCI/native/recovery as applicable |
|3 DB/Native/I/O|CONDITIONAL,P1/P2|Per-operation causal cost and durability review|Explicit IMPLEMENT/DEFER/NOT REQUIRED per chmod/PRAGMA/projection/readback/scrub/temp/config/health candidate; matched cost and safety evidence |
|Long-term stability|CONDITIONAL observation,P1|Separately approved low-overhead natural workload|RSS/PSS/cgroup/cardinality/daily tasks/CPU/SSD/latency; execute/defer disposition; no invented leak/wear/soak |

The historical6004400 packages1/2 and conditional3 differ from these numbers. Its schema24 and old identity/recovery assumptions cannot be replayed into schema25. Deep-audit C1/C2 closed in currentPackage1; C5 belongsPackage2; C3/C4/C6/C7 remain conditionalPackage3. No Package2/3 changes here.

Operational closure requires all eight user criteria: accepted1; decision/acceptance2; explicit disposition3; performance-related legacy disposition; residual CPU/RAM/SSD risks; matched baseline comparison; applicable staging/native/fullCI checks; no functional/security regressions. Optional long-term work needs execute/defer, not fabricated PASS. Stage5 remains unopened until closure.

## 4. Legacy register and TOP candidates

[Full single registry](LEGACY_REGISTER.md) includes concrete functions/lines, call/installer/startup/migration/recovery reachability, compatibility/data dependencies, removal risks, evidence gaps and required tests. SAFE TO REMOVE is empty because external/dynamic/upgrade consumers are not ruled out. This is a useful negative finding, not permission to delete plausible dead code.

| Candidate | Disposition / owner | Expected benefit / risk |
|---|---|---|
|Expired cache entries / shared probe cache|Measured retention mechanism; Package2, not legacy removal|Bound retained RAM; no proven live leak; invalidation/concurrency/native-proof risk |
|Repeated SQLite setup/chmod/PRAGMA|UNKNOWN removal economics; conditionalPackage3 + DB/6A|Synthetic498chmod/500PRAGMA per100sessions; chmod fewms, production rate UNKNOWN; permission/FK/durability risk |
|Unchanged startup scrub temp output|KEEP security scan; conditionalPackage3|Synthetic unchanged1.03MB/pass andfsync; avoid temp I/O only with redaction/append/atomicity/error equivalence; no steady-state SSD claim |
|Recovery alias `recover_startup_routing_to_direct`|UNKNOWN; Stage6|Only definition/test refs found; external callers unknown; negligible/unmeasured CPU benefit |
|logs/events overlapping APIs and adapters|NEEDS MIGRATION; Stage5/6|Possible future simplification; actual duplicate request cost UNKNOWN; clients/historical data risk |
|`include_legacy` subject option/taxonomy normalization|NEEDS MIGRATION; Stage5/6|Persistent compatibility, not proven no-op; data/upgrade risk |
|Mihomo fast sentinel,watchdog/Xray facades|KEEP; Stage8→7|Active verified fallback/import seams; no measured savings from deletion |
|Schema7→25 migrations andv24→25 offline command|KEEP; Stage5/DB|Required supported upgrade/recovery; removing can lose credentials or fail startup |
|Obsolete deployed-path cleanup list|KEEP; installer/Stage6|Removes stale importable installed modules; absence in Git is not absence in upgrades |
|Old6004400 optimization aggregate|KEEP evidence, not executable plan|Avoid stale replay; no measured benefit from deletion/replay; schema/identity regressions possible |

## 5. Measured resource relation and early proposals

Only previously measured synthetic costs justify early proposal: cache retention (1000TTL0 keys retained1.024MB), SQLite setup counts (profiled100sessions73.45ms; batching jointly changes transactions and setup, fsync4→4), startup scrub unchanged temp+fsync. These are not evidence that historical migrations/facades/endpoints cause productionCPU27% or physical SSD wear. Exact stack causing rare health tails, legacy row volume, deployed client usage and migration cost remain UNKNOWN.

Potential early source packages require separate approval and isolated matched tests; no removal now. No global connection/cache, delayed durable writer, volatile credentials/checkpoints, lower WAL/fsync/synchronous, arbitrary timeout/memory limit change, or less native proof. Preserve Core ownership,CAS/revisions/incarnations,last-good/atomicity,78 durable identities, fail-closed/explicitDIRECT, DNS and independent Rescue/Tailscale.

## 6. Ownership, readiness and remaining work

Package2 is ready for separate scoped design/implementation approval, not automatically started. Broad dead/obsolete compatibility remains singleStage6 backlog; Stage5/DB own data contracts and support floor; Stage8 defines optional replaceable/multiple module/connection contracts beforeStage7 extraction; Stage6A owns later measured execution/orchestration. This audit starts none of those stages.

Remaining: causal memory/cardinality attribution and daily-cycle observation, actual permission/no-op-write/controller call rates, SDK/alternative/absent-module application matrix, controlled external E2E and full host/release gates. Public credential historical exposure remains separately planned; no sensitive value or credential rotation here.

## 7. Verification and delivery

Documentation-only branch from main3f96e1e. Static links/Markdown, package/stage decision consistency, history/pending-patch preservation and changed-file secret scan required before commit. Full required PRCI must pass on final exact HEAD using existing full-acceptance policy; no workflows or test assertions changed. Final run/status goes into PR body and a local final receipt without changing qualified source. No merge/direct-main push or production mutation.
