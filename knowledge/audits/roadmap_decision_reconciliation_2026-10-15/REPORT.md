# Roadmap and decision reconciliation — 2026-10-15

Documentation-only follow-up within PR11 `fix/test-data-isolation-live-acceptance`, source baseline564f715. No new stage/branch/PR or production operation. Historical reports and raw evidence remain untouched. The final full required check must qualify the documentation commit's own HEAD; ancestor CI is historical evidence only.

## Before and preserved history

Canonical was133608 bytes (SHA256 `8325c21fd6a3dfdd1ab6b15ac1fb768834fd9c59e3bf3968cf68724f8587e129`); English mirror114475 bytes (`def3a4a941edcdcb4dcf1df9a08f6eb8123fc54482dcc85a53431bc162f836d6`). Their active states diverged: canonical closed CI; mirror/CI map still awaited PR5 merge/main, and both had numerous superseded current-status headings. Dates/file length were not used as authority over newer accepted evidence. Canonical defined the owning plan and stage order; accepted main/PR/live receipts and current user checkpoint resolved statuses.

Exact originals are retained in the respective `history/ROADMAP_PRE_2026-10-15_DECISION_RECONCILIATION.md` files. Only the already-public mirror snapshot is versioned here; the larger owner-local canonical snapshot stays outside Git. Historical text and location-relative links are preserved verbatim, not treated as active instructions. The reviewed public active plan is committed/pushed and full required PR CI verified before publishing the matching canonical body using a pre-edit hash comparison; concurrent canonical edits must not be overwritten. Titles identify canonical/mirror roles; active content must match after normalizing location-specific link roots.

## Existing, added and corrected

| Direction | Previous coverage | Reconciliation |
|---|---|---|
| CI/Git/release policy | Present, conflicting stale promotion states | CLOSED CI from actual PR5/main38363962787; current PR8/10/11 source versus deploy/live table; preserved event policy and squash/user-merge/deploy boundaries |
| Operational resource audit | CPU/RAM/WAL/SSD/wakeups existed, old package sequence stale | Mandatory re-audit after PR8/10/11 before old fixes; add CPU time/IOPS/fsync/device/NAND writes/amplification/endurance attribution and bounded RAM-cache durability constraints |
| Timeouts/thresholds | Dispersed40/45/120/20 references | Existing Operational subitem with provenance/meaning/environment/headroom/cleanup audit; no value change or invented universal SLA |
| Core/Modules/SDK | Stage8→7→post-extraction/late SDK present | Explicit independent Core, optional/replaceable runtimes, multi-instance/same-provider/independent API/connections scoping; full app matrix remains unqualified |
| Reference architecture | No qualified post-extraction study task | Study-only3x-ui/analogs comparison under existing post-extraction follow-up; strict extraction+audit dependency, no integration/vendor dependence |
| Server-wide Compose orchestration | Docker cleanup inventory only | Stage6A planning, stable-module implementation dependency and Stage9 qualification; independent authorized Docker API and no circular runtime/lifecycle dependence |
| DNS/cleanup/public docs/config/security/host | Existing functional owning sections | Retain detailed acceptance, add Codex/raw-evidence separation, whole-server/late safe cleanup and clear host versus namespace boundaries |
| Test data/live acceptance/security | Existing local audits and PR11 checkpoint | Separate synthetic function, real state and authorized controlled E2E; register upstream public UUID overlap without value, UNKNOWN origin, non-intersection with original78 and plan-only rotation |
| Project map current state | Held API/unrestored identities/pre-merge CI pointers | Dated current clarification and historical labels; preserved lifecycle contracts and old report wording |

No new numbered stage: original16-item order remains; Stage8 contracts precede Stage7 physical extraction, Post-Extraction Audit follows, and reference research opens strictly afterward. Operational acceptance precedes Stage5; database/Stage6/6A, late metrics, UI/docs and Stage9/10 retain their positions. New subitems are not artificial blockers of the closed CI milestone.

## Decisions and remaining unknowns

[Decision register](../../PROJECT_MAP/DECISIONS/ROADMAP_DECISION_REGISTER_2026-10-15.md) indexes25 agreements with status/priority/dependency/entry/exit evidence; detailed backlog remains in [roadmap](../../ROADMAP.md). Current facts: productionffe506e, original78 restored; PR11 source not deployed; Synthetic Functional PASS, Production State PARTIAL, External E2E NOT RUN, overall production BLOCKED. Public credential origin/ownership and external misuse remain UNKNOWN; fixture replacement does not revoke history, no rotation performed. Logged tokens and upstream credential are distinct from the original78 inbound identities.

Unconfirmed: healthy authorized external upstream for positive live E2E; complete host/systemd/rollback/boot acceptance; full alternative/absent/multiple-active module application matrix; current SSD/IOPS/fsync/endurance metrics and limit design rationale; actual server-wide orchestration placement/authorization and migration. These have entry/evidence requirements, not fabricated PASS. This documentation update starts none of them.

## Verification and publication boundary

Checks: compare archived byte hashes; validate active mirror/canonical-proposed content identity after link-root normalization, stage order,25 register IDs, scoped Markdown links/headings and completeness; run existing documentation-affected static/isolation/gate contracts; verify changed-file surface is documentation only and no raw/private files staged. Historical snapshot links retain original-location context and are excluded from current-link validation. Then commit/push only this existing PR branch with `ci:full-acceptance`, inspect actual current-HEAD full safe L0–L5 JUnit/source/plan/cleanup and required checks. No L6/L7 or runtime probe/deploy is authorized by these checks. Final source-bound GitHub run/status is recorded in the PR and local final reconciliation receipt after completion; it is not inferred from the prior1643/0/0 run.

Files: active English roadmap; exact prior mirror snapshot/history index; decision register/index; targeted PROJECT_MAP current pointers; this report. Canonical/history updates remain external, no private canonical dump copied to the public repository. No AGENTS/source/workflow/runtime/secrets changes. Pre-existing untracked handoffs/evidence/pending documentation patch remain local and unmodified.

Local verification result: manifest172/172; contract suite266/266 PASS in the project test venv; isolation18/18 PASS; clean-tree surface and diff whitespace PASS; snapshot hashes,25 unique decisions, required topics, normalized link-root content and scoped local links PASS. An initial system-Python attempt could not import pytest (four import errors); rerun in the existing test venv passed, with no dependency or runtime changes. Full required PR CI remains a separate current-HEAD check.
