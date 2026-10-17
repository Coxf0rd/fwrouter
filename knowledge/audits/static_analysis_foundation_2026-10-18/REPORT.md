# Static Analysis and Linting Foundation

## Source and scope

Base main is `aff66d77a3cb55b2a6a1f65e4bd14fb49af7d23c`, the single-parent squash of PR19. Its tree equals qualified PR19 head7bac8ac. Actual main push CI39069136127 and dependency publisher39069136099 succeeded at that exact SHA. The downloaded aggregate is eligible, has no blocked reasons and reports main smoke passed. This prerequisite was verified before creating `ci/ruff-static-analysis-foundation`.

This is a source/CI foundation task. No production inspection, deployment, restart, provider refresh, native apply, database mutation or credentials operation is performed. Historical audits, untracked private evidence and pending documentation patches are preserved. No new numbered roadmap stage is introduced.

## Existing checks and confirmed gaps

Ruff was already hash-pinned at0.17.0 in `tests/gates/requirements-ci.txt`; L0 and the diagnostic `lint-changed` command used an isolated changed-file F821 check, bounded subprocess execution and a positive/negative undefined-name canary. L0 also parses changed Python syntax; workflows already use pinned actionlint for YAML workflow syntax. The static gate did not enforce unused imports/variables or check undefined names in untouched files. L0 failure also did not stop the routine-suite loop, although required aggregation eventually failed and prevented hosted acceptance.

The backend dev range allowed Ruff>=0.6,<1 while `backend/uv.lock` resolved0.15.12. The broad backend config declared E/F/I/B/UP/SIM, but the actual gate explicitly overrode it with F821. Thus the declared broad configuration was not evidence of broad lint execution, and local dependency resolution could differ from CI. Mypy strict configuration and a locked dev tool existed, but no actual type gate existed.

PR19's missing Path annotation/import and similar undefined names are statically detectable. Incorrect imports from an existing but wrong module, missing dynamic facade exports, ownership errors, dictionary-state semantics and native/recovery correctness are not generally proven by Ruff. Lint must complement, not replace, component/native proof and independent review.

## Measured baseline at main aff66d7

Tracked Python inventory:479files —388backend,41tests,7installer,3host,2integrations,38historical knowledge/audit scripts. The maintained-source baseline covers441files; untracked/ignored evidence and historical knowledge scripts were not traversed. Changed historical Python must still receive strict changed-file checks when deliberately edited.

| Rule class | Existing diagnostics | Disposition |
|---|---:|---|
| F821 undefined names | 5 | Small confirmed import defects targeted for correction; global critical gate must become clean |
| F401 unused imports | 759 | Existing unchanged debt, not declared accepted or globally lint-clean |
| F405 names from star imports | 137 | Dynamic facade/compatibility debt; requires owner-specific review |
| F841 unused local variables | 35 | Existing unchanged debt; new/changed files must be clean |
| F811 redefinitions | 12 | Primarily duplicate imports; one facade re-export, not automatically a proven runtime incident |
| F403 star imports | 8 | Interface/compatibility debt, no bulk rewrite |
| F541 redundant f-string prefix | 6 | Low-impact existing debt, no cosmetic repository sweep |
| E9 | 0 | Keep syntax/error checks; no extra baseline defect found |

Total F/E9 baseline962diagnostics across441files; measured source-only scan approximately0.15s. The previous broad selection produced21503diagnostics, including19314E501 and748I001. Enabling broad style/formatting now would create unrelated churn. The formatter was assessed read-only on eight immutable main source/tooling files:seven would reformat at Python3.11/100columns; no formatting was applied. Formatter enforcement is deferred. These measurements are local source-tool timings, not hosted performance or production resource savings.

Five F821 findings: missing XrayClient annotation import in xray_noop; missing ReconcileResponse annotation import in diagnostics; missing Any in watchdog_manual_flow; missing pytest in a deferred test callback; and a missing `_reconcile_vpn_runtime_for_apply` import in the server-override handler's selector-not-found recovery branch. The latter can raise NameError if that branch executes; no past production occurrence is claimed. A regression must fail on the original missing import before the intended existing helper is imported.

## Selected enforcement model

Extend the existing gate and jobs, not a parallel lint pipeline. Use the same exact Ruff version and root configuration locally and in CI. Global critical undefined-name checks apply to all maintained tracked Python. Strict whole-file F/E9 applies to every new/modified tracked Python file, including deliberately edited historical scripts. Inline noqa cannot hide violations. Existing unchanged F401/F841/star-import debt remains explicitly unqualified; there is no blanket suppression or growing baseline allowlist. Future cleanup is incremental when files are touched, without a new architecture-refactor backlog.

The developer command must include committed changes since the immutable base and indexed/tracked local edits; new files need indexing rather than a recursive scan of private untracked evidence. Unknown/missing/wrong-version tools, invalid inventory/configuration, truncated output and detected violations fail nonzero. Add the existing lint entrypoint immediately after immutable-base resolution in the current push/PR-main jobs so static failures stop before costly routine/native/browser execution. L0 still records its own mandatory static evidence. CPU, memory, startup, native receipts, strict aggregation and required-check identity remain unchanged.

## Type-checking decision and concrete next step

Mypy1.20.2 is already in the backend lock. An isolated feasibility run on six real Core/generation/native/repository targets emitted1484diagnostics in122transitive files. Restricting displayed diagnostics with follow-imports=silent still yielded161in the six targets, cold5.683s and215336KiB child max RSS. That mode was diagnostic isolation, not a proposed suppression policy. Supporting runtime libraries in this probe came from existing version ranges rather than the complete project lock, so the result is feasibility evidence, not a reproducible CI receipt.

Dominant causes are dynamic private facade exports, Any/dictionary boundaries, nullable/union narrowing and missing PyYAML stubs. High annotation ratios do not establish useful state/ownership type coverage. Pyright was not executed; no superiority/performance claim is made. A strict backend-wide typing gate is deferred rather than manufacturing PASS with blanket Any/ignore_missing_imports.

Recommend a separately scoped pilot using the existing locked mypy on dependency-light typed contracts/leaf modules, then explicit facade exports and discriminated DTO/Protocol boundaries owner-by-owner. Positive/negative type fixtures and exact lock/source bindings are prerequisites. Larger persistence/Core-module interface changes stay with their existing owning roadmap milestones. No parallel generation/admission system or typing-driven business-logic refactor is introduced here.

## Verification and remaining gates

Implementation and local targeted checks are complete at this pre-QA checkpoint; independent review and final exact-HEAD mandatory PR CI remain pending. Developer instructions are in [STATIC_ANALYSIS.md](../../STATIC_ANALYSIS.md). Final commit/PR/run, counts and source/plan/profile/native/JUnit/cleanup evidence are recorded in the local FINAL_HANDOFF after qualification; no earlier PASS is reused for a changed HEAD.

Tool references: [Ruff rule catalogue](https://docs.astral.sh/ruff/rules/), [Ruff configuration](https://docs.astral.sh/ruff/configuration/), [mypy gradual adoption](https://mypy.readthedocs.io/en/stable/existing_code.html). Actual enabled rules and behavior are qualified against the pinned local tool, not inferred from changing defaults in online documentation.


## Implemented package and local qualification

The shared root `ruff.toml` selects F/E9 and requires exact0.17.0; backend configuration extends it and the dev extra pins the same tool. The backend lock was generated/checked with uv0.13.1. Its serializer updates lock revision3to5; parsed package inventory remains36entries and Ruff is the only package-version change. CI's existing hash-locked Ruff wheel is unchanged. The developer guide states the compatible uv requirement explicitly. No acceptance image allowlist or production installer change is needed: lint runs on the full checkout before runtime/test packaging.

The existing gate now checks global F821/F822/F823 and strict whole-file F/E9 on committed plus local indexed/tracked edits. It refuses malformed inventories, traversal/symlinks, wrong/missing tools and truncated output. Positive/negative canaries actively exercise undefined names, undefined exports, unbound locals, unused imports/variables and syntax errors, including attempted inline noqa. Both current workflow jobs invoke that same entrypoint before planning/routine execution; no new job/check or parallel pipeline was added.

Five F821 findings were corrected with imports; the selector-not-found handler regression executed its real handler/common helper and failed on the original missing name before correction. Existing Xray noop module constants are explicitly re-exported, preserving their attributes rather than deleting compatibility because internal references were absent. Related touched-file corrections remove one unused shlex import, one unused local and one duplicate re import. In total11diagnostics are resolved:5F821,4F401,1F841,1F811.

Remaining unchanged F baseline951:755F401,137F405,34F841,11F811,8F403,6F541. None is claimed accepted, and no allowlist suppresses new/changed files. All441maintained files are clean for the global critical-name rules; all8changed Python files are clean for full F/E9. The measured complete local lint command, including git inventory/version checks and canaries, took0.376s; the baseline0.15s scan had a smaller scope of work and is not a like-for-like timing comparison.

Local test-infrastructure L1 plan completed all8suite executions; its14reported exact node statuses all passed (some stdlib unittest suites are encapsulated, so this is not a count of every nested unittest). Gate-contract contained80unittest cases. Three exact isolated backend regressions passed with no skips, matching node inventory, sanitized JUnit and owned cleanup. Manifest validation covers178test files. Isolation smoke18/18checks, owned acceptance smoke, lock check and whitespace/surface checks passed. No local Docker/native processes or production checks ran.

A mypy feasibility invocation created an ignored source cache; the source-surface guard detected its cache.db. Only that tool-owned cache was quarantined outside the repository with its evidence preserved. Subsequent surface check passed. Future type experiments must use an explicit private cache directory; this did not change source code or production state.


## Independent review correction before hosted CI

Initial frozen review independently passed actual Ruff/canaries,80gate unittest cases and the three backend regressions, but returned CHANGES_REQUIRED for a local false blocker: a Python file committed as changed since the base and subsequently deleted in the worktree remained in the committed-path set. Requiring it to exist rejected a legitimate deletion. Clean hosted checkout was unaffected; the developer command still needed correction.

The related regression failed first for staged deletion, unstaged deletion and staged rename. Inventory now obtains explicit local Git D paths with rename detection disabled, validates those paths structurally under the same bounds, and subtracts only confirmed deletions before checking remaining path existence. Unknown missing paths, malformed UTF-8, truncated/failed Git output, traversal and symlinks still fail closed. This is not an unconditional ignore of absent files.

The corrected group passed all8L1suite executions/14reported node statuses, including81gate unittest cases; three exact backend regressions passed with sanitized JUnit and cleanup. Actual lint/canaries, manifest178files, isolation smoke18/18 and owned acceptance smoke passed. Independent final review and the first hosted mandatory run remain pending at this pre-run checkpoint. No expensive CI was launched during the audit/fix iterations.

## First hosted attempt and planner coverage correction

The first pushed source a493b64 passed the new actual Ruff gate in both push39072739978 and required PR39072747884. Both stopped before any routine/native execution because the existing fail-closed planner had no domain rule for backend/uv.lock. The previous local infrastructure-subset plan did not exercise the full committed PR diff; this was a qualification gap, not a Ruff failure. Failure logs are preserved privately.

A regression first reproduced the missing lockfile mapping in quick and mandatory planning. The lockfile now has the same explicit test-infrastructure ownership as backend/pyproject.toml. The complete immutable-base diff is also checked in both actual plan modes: all17changed paths have rules, quick selection95files, mandatory selection177files including9native and1explicit optional L7 deferral. Required primary levels remain L0-L4 with L5 anchors; no required suite is suppressed. Gate contract82cases, manifest178files, actual lint and whitespace checks passed after the grouped correction. Final independent re-review and a fresh exact-HEAD required CI remain separate gates.
