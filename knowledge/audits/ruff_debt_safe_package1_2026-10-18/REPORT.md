# Ruff Lint Debt Cleanup — Package 1: Safe Fixes

## Prerequisites and scope

Base is main `4f0c43cecb3b7515be111fe169038ecdbdad6df0`, the single-parent squash of PR20 (parent aff66d7). Its tree equals qualified PR20 head a9bf34d. Actual main CI39074290982 and dependency publisher39074291003 succeeded at this exact source. Branch: cleanup/ruff-debt-safe-package1. The existing canonical roadmap and pending local documentation patches are preserved; this is authorized lint maintenance, not a new numbered stage.

Production is not inspected or changed. Deployment, restart, database/credential changes, native apply, routing and generation/recovery behavior are outside scope. Historical knowledge scripts and private/untracked evidence are not cleanup targets. The existing Ruff0.17.0, full changed-file F/E9 gate, global critical-name checks, immutable source bindings and all native/CI gates remain unchanged.

## Reproducible baseline

Pinned Ruff0.17.0 scans the same441maintained tracked Python files, using root ruff.toml, F/E9, --ignore-noqa and --no-cache. Source inventory comes from Git rather than recursive filesystem discovery. Baseline951diagnostics across93files: F401755, F405137, F84134, F81111, F4038, F5416. Component attribution: backend934, tests14, installer2, integrations1. The441source files comprise backend388, tests41, installer7, host3 and integrations2.

Full per-file/rule findings are retained privately in /root/fwrouter-recovery/ruff-debt-package1-20261018/baseline-ruff-F-E9.json and baseline-summary.json. These contain source diagnostic locations, not production data; no raw/private evidence is published. The before/after comparison uses the same source inventory, rules and executable. A lower finding count is not a runtime performance claim.

## Safety classification and selection

A Ruff automatic-fix flag is not evidence of behavioral safety. Candidate F401 changes require review of public exports/__all__, package initialization, decorators/registration, dynamic consumers, plugin loading and import-time effects. Imported compatibility facades and ambiguous project-local dependencies remain unchanged. F403/F405 are deferred to Package2 with owning interface review.

Only exact string-equivalent F541 changes, proven unnecessary imports, accidental unconditional duplicate imports of the same object and side-effect-free unused bindings may be selected. A touched file must be wholly F/E9-clean without noqa or blanket suppression. If unsafe findings would remain in it, the complete file is deferred rather than weakening enforcement. No bulk ruff --fix or repository formatting is used.

## Implementation and qualification

Pending the grouped Writer package, independent QA and final exact-HEAD mandatory PR CI. Final per-rule counts, selected/deferred file register and test evidence will be appended before source freeze; final commit/run metadata is retained in the local FINAL_HANDOFF and PR to avoid an endless documentation-only source/CI cycle.

## Completed safe package

Fifteen Python files change; the final frozen file register and all deferred findings are in [DECISION_REGISTER.md](DECISION_REGISTER.md). Seven F401 findings are removed only in test modules (unused json and datetime imports). Seven F811 findings remove second unconditional imports of the same initialize_database/deepcopy objects; the first binding and original module initialization remain. Four F541 strings have no interpolation/escaped-brace semantics and retain identical values. Two F841 exception bindings in routes/system.py are removed without changing exception matching, logging, HTTP errors or handler order. No call/query/validation expression is dropped.

| Rule | Before | Fixed | Remaining |
|---|---:|---:|---:|
| F401 | 755 | 7 | 748 |
| F405 | 137 | 0 | 137 |
| F841 | 34 | 2 | 32 |
| F811 | 11 | 7 | 4 |
| F403 | 8 | 0 | 8 |
| F541 | 6 | 4 | 2 |
| Total | 951 | 20 | 931 |

Global F821/F822/F823 and E9 remain clean. The same441-file scan and all15changed whole-file checks pass; no tool/rule/config/timeout/resource-limit changes or noqa exceptions are introduced. No new functional defect is claimed; these are redundant bindings/literals, not evidence of production incidents or performance savings. New implementation-mirroring tests were avoided; existing handler, runtime/config and test-discovery checks provide the relevant equivalence evidence.

### Local qualification and honest failed attempts

A first broad ad-hoc routine pytest invocation reported438PASS/7FAIL. Three failures involved operations not permitted by that routine isolation profile (host temporary file/subprocess expectations). Four Xray rollback assertions reproduced on the source-only immutable baseline and current source; the current failure was additionally reproduced with hash-locked CI dependencies. Exact node coverage, sanitized JUnit and owned cleanup were retained for these failures; they are not marked PASS. The cause of the baseline rollback failures in that isolated invocation remains UNKNOWN.

Fresh main CI artifacts clarify the coverage boundary: test_xray.py is profile-bound, the routine run reports only two explicit overrides, and qualified-child JUnit covers selected nodes rather than those four rollback nodes. A green mandatory CI therefore does not establish that these four isolated tests passed. To keep this package strictly qualified and cosmetic, the proposed duplicate-import cleanup in test_xray.py was completely withdrawn; it is byte-identical to main. The remaining F811 is explicitly deferred. No recovery code/test assertions or selection contracts were changed to manufacture PASS.

The final targeted synthetic group completed11PASS/0FAIL/0SKIP with exact-node coverage, sanitized JUnit and owned cleanup. Existing route-error handling passed before and after(1node each); smoke contract14/14PASS. These counts are separate from failed exploratory attempts and the later final mandatory hosted run. Independent QA, final committed-source bindings, native/packet receipts and required PR CI remain pending at this pre-freeze checkpoint. No local Docker/native or production operation ran.

### Package2 handoff boundaries

Deferred F401 requires public facade/__all__/registration/dynamic-consumer and side-effect tracing; F403/F405 needs interface ownership review rather than import-star rewriting. The32remaining F841 include queries/calls/projections, fixtures and possibly omitted assertions; their values must not be discarded before intent is established. Four F811 remain in mihomo_config, test_provider_recovery, test_routing_global_wave1 and test_xray; containing-file debt or qualification issues prevent a complete safe edit here. Two F541 remain in files with other ambiguous F401 findings. The per-file register names all remaining affected source files and counts. Optional native modules are not obsolete and no API compatibility is removed. Package2 is not authorized by this report and is not started.

## User-authorized extension in the same PR — difficult debt and confirmed defect

After Package1 qualified at19e1148, the user explicitly requested continuing all related fixes in the existing branch/PR21, superseding the earlier separate-Package2/merge gate. PR21 was returned to Draft. Earlier safe-only counts and CI remain historical evidence for19e1148; they are not final qualification of this extension. Production remains untouched.

### Confirmed functional root cause

A real SQLite regression seeded pending recovery via the existing state owner and called idle_active_failure_confirmation. Before the fix, active_quality_degraded_confirmation replaced the traffic_recovery candidate and lost its recovery_pending marker in durable state. The correction preserves that owner candidate while recovery is pending instead of allowing an idle observation to supersede it. The regression verifies the complete durable candidate, reload via SQLite and subsequent compare-and-set advancement of the same pending operation. No native apply, routing policy, credential or generation manager is introduced. This makes the final PR a lint cleanup plus a specifically documented recovery-state fix; it is no longer represented as entirely cosmetic.

The proof-only Xray stager test captured original_active but never checked it before overwriting active config as setup for a second scenario. The new immediate byte-equality assertion closes that test gap; no production stager behavior is changed.

### Difficult-debt choices

Three wildcard-import consumers (UI clients/inventory and apply_orchestrator_drift) now name their actual dependencies explicitly. Central public facades, dynamic dependency injection and existing monkeypatch seams must remain available. Other changes remove ignored return bindings while preserving validation/save/refresh calls, accidental duplicate imports and proven redundant pure computations. The unused subscription_pipeline details dict is retained: deleting it could change reads/error behavior on malformed objects. Large unresolved central facades and unqualified native/recovery files are explicitly not deleted merely to satisfy Ruff.

The same441-file pinned-Ruff inventory changes931→707: F401748→683, F4038→5, F405137→1, F5412→2, F8114→2, F84132→14. Compared with original main4f0c43c baseline951, the consolidated PR removes244diagnostics. Every touched Python file must pass fullF/E9 without noqa/config changes; globalcritical checks remain enabled.

### Pre-QA qualification and limitations

Official isolated exact-node execution:529backend PASS/0FAIL/0SKIP with exact-node coverage, sanitized JUnit and owned cleanup. Official test-infrastructure gate:14reported nodes PASS, L0PASS, no blocked reasons. Initial direct pytest attempts used the wrong isolation wrapper for stdlib contract tests and are retained as NOT ACCEPTED evidence, not passed qualification. Source hashes and detailed results are frozen privately under /root/fwrouter-recovery/ruff-debt-package2-20261018/FROZEN_SOURCE.json. Independent QA and new exact-HEAD fullmandatoryCI remain pending; previousCI39076593577 does not qualify new code. No production/deployment/native apply or next roadmap milestone follows from this source work.

### Independent QA correction: durable concurrent ownership

First independent review returned CHANGES_REQUIRED and deterministically reproduced a concurrent lost update: active quality read an empty snapshot, another writer persisted recovery_pending, then the stale unconditional UPDATE replaced it. The initial in-memory guard was insufficient. A real-SQLite barrier regression failed before correction. The existing watchdog runtime state owner now accepts optional expected candidate/path/last-decision conditions and executes a single conditional SQL UPDATE; conflict/write failure returns no successful publication. Active-quality write/clear paths supply the observed snapshot and publish their in-memory candidate only after CAS succeeds. Both paths refuse preexisting pending dictionaries regardless of candidate kind. Strict read mode prevents read exceptions from being converted into authoritative empty state; default reader compatibility remains unchanged for other callers. No migration or parallel owner is added.

The corrective group passed279isolated exact tests(0FAIL/0SKIP), including real concurrent marker creation/phase advancement, owner preservation, stale/DB-error paths and a mutating-stager negative canary. Exact-node coverage, sanitized JUnit and owned cleanup passed. Earlier unchanged-module targeted evidence remains applicable only to unchanged file hashes; no duplicate test counts are summed into a fabricated total. The final independent re-review and exact-HEAD hosted gate remain pending at this source checkpoint.

Final corrected source review: independent QA PASSED. It independently checked8race/error/admission cases and4facade/UI cases, reviewed279targeted writer results and verified strict touched-file Ruff/source hashes. Final count707(F401683,F4035,F4051,F5412,F8112,F84114),244removed from original951. Main requiredCI for the newHEAD is still pending; original19e1148qualification is historical only.
