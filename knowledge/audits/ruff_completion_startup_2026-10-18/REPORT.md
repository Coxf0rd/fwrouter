# Ruff Debt Completion and Startup Performance Investigation

## Source, authority, and safety

This task starts from main `47cfd63fc220f2890e140da27ff54c91a8a2c7a1`, the single-parent squash of PR21 (parent4f0c43ce). Its tree equals qualified PR21 final head460f111. Actual main CI39084314190SUCCESS was verified with mandatory_all, eligible aggregate, empty blocked/missing levels, main smoke passed and retained JUnit without failures/errors/skips before creating cleanup/ruff-completion-startup-audit. Historical reports, private evidence and pending roadmap patches are preserved. No production inspection, deployment, restart, recovery, realDB/credential/provider/native-routing/host change is performed.

The work has two distinct streams: contract-preserving Ruff cleanup and evidence-based startup investigation. A smaller lint count is not a performance result. No timeout, memory budget or safety proof is relaxed. No new roadmap milestone or large architectural refactor is started.

## Baseline

Ruff0.17.0, F/E9, --ignore-noqa, no-cache; same441tracked maintained Python files, historical knowledge/untracked/private evidence excluded. Baseline386findings in45files: F401382,F5412,F8111,F8411; F403/F405zero. Package1/2 reports remain historical checkpoints, not current uncommitted completion claims.

## Initial startup evidence — not a causal comparison

Five qualified artifact sets share the20,722,965-byte config/digest,199,257rules,82Xraybindings/identities,7subscription profiles/70nodes, native binaries, dependency image/lock and2.5GiB cgroup ceiling. They are still one observation per source/run, without repeated matched samples or complete runner CPU/throttle attribution.

| Source | Startup wall seconds | API CPU at readiness seconds | CPU/wall |
|---|---:|---:|---:|
| a9bf34d | 26.4467 | 20.40 | 77.1% |
| 19e1148 | 34.0328 | 26.59 | 78.1% |
| 5b07934 | 43.8207 | 34.17 | 78.0% |
| 460f111 | 42.9249 | 33.23 | 77.4% |
| main47cfd63 | 43.2814 | 33.59 | 77.6% |

Dominant nested wall paths are startup Xray subscription profile recovery(15.53→23.78s) and persisted live-routing recovery(6.73→12.82s). Existing phase CPU instrumentation uses process_time, so concurrent CPU and nested wrappers cannot be summed as exclusive function costs. An immediate first watchdog scheduler tick may overlap readiness when the sanitized worker defaults enable it; captured evidence does not prove its contribution. This remains a hypothesis, not a demonstrated regression or reason to disable safety work.

## Pending qualification

Per-file import/export decisions, F811/F841 contract analysis, matched repeated benchmarks, independent review and final exact-HEAD mandatory CI remain pending. Measurements must bind workload/profile/source/image, retain distributions and raw evidence privately, and distinguish phase instrumentation from non-invasive timing. If no causal local regression is proven, no speculative runtime optimization will be introduced. Final PR/SHA/run and limits will be recorded in local FINAL_HANDOFF and the PR after qualification.

## Ruff completion implementation and first independent review

The386findings were resolved without disabling rules:382F401 imports were either retained as explicit existing module-level reexports/initialization contracts or removed only after consumer/side-effect tracing; Mihomo's duplicate imported helper was removed while retaining the actual local facade definition; two truly interpolation-free strings lost redundantf prefixes; the L7unused read targeted subscription rows unrelated to the tested Xrayemail and was removed rather than inventing a false cross-table assertion. Existing native deletion/no-resurrection assertions remain, and destructiveL7 isNOTRUN.

The global maintained-source gate now checks fullF/E9, not only critical undefined names. Negative canaries include unusedimports/locals and corrected unboundlocal detection; inline noqa cannot bypass it. Baseline0/441 was verified before strengthening; historical/untracked scope remains unchanged. Public star namespaces, direct helper attrs and monkeypatch seams remain explicit contracts. An import-order side effect in test_performance_optimizations was retained through explicit import_module initialization before the circular reconciler import.

First independentQA returned CHANGES_REQUIRED: an overly broad f-prefix cleanup removed actual auto_eligible_sql interpolation from the first subscription query, yieldingSQLite literal-brace errors and46cascading tests. PinnedRuffalone stillpassed; realexecution caughtthedefect beforepush. Correction restored that helper and first f-string; only the second non-interpolating proxyquery and the Xraysubscription proxyquery becomeplainstrings. A realSQL regressionfailedbeforecorrection;78subscription/exclusive/customproxy tests passedafterwards. ASTinspection of all changedPython paths found onlythose two interpolation-freeJoinedStr removals.

The same424node affected isolated runner aftercorrection reports419PASS,4baseline recoveryassertionfailuresand1qualified-subprocess denial. Thosefourassertions reproduce identically on a syntheticimmutable47cfd63archive with the same runner. They are documented as baseline/unqualified, notmaskedorexempted orcountedPASS; sourceCIselectedprofilecoverage remainsseparate. Newcorrectedfreeze andindependentreview remainrequired. No productionoperations.

## Frozen package review and matched comparison protocol

Corrected lint review PASSED: the 47-file implementation freeze was independently checked, including real affected execution paths, 83 gate contracts and pinned Ruff. The maintained inventory now includes the new diagnostic helper: 442 Python files, zero F/E9 findings. The four baseline recovery assertions and the qualified-subprocess denial above remain explicitly unqualified locally.

The initial watchdog-overlap hypothesis is withdrawn as an explanation for the dominant bootstrap phase growth: scheduler startup follows those phases. No runtime performance optimization has been implemented.

The independently reviewed diagnostic uses the existing isolated Phase D launcher and nine sequential attempts in a balanced three-arm order (three samples per arm). The historical arm is reachable main ancestor 4f0c43cecb3b7515be111fe169038ecdbdad6df0, whose tree equals the originally measured a9bf34d tree; the other arms are main47cfd63 and the frozen candidate. Executed workloads, fixtures, dependencies and native binaries are bound and compared. Individual startup and memory limits remain unchanged. Source-specific launcher workspace binding preserves the existing path qualification. Launcher timeout requests owner cleanup and stops further scheduling; incomplete cleanup is UNKNOWN, never PASS.

Harness QA PASSED with 154 acceptance contracts, 83 gate contracts and pinned Ruff; actual pinned actionlint also passed. Hosted attempts have not yet executed at this checkpoint. Collected phase CPU is process-wide, not exclusive thread CPU. Allocations are not measured. Outer cgroup I/O is aggregate runner activity, not application-only or NAND writes. Results must retain every attempt, failures and distribution before any causal claim.
