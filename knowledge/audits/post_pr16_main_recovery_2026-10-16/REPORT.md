# Post-PR16 main CI recovery and rollout gate

## Verified integration

PR #16 merged as `65f5c9f332d9eab194b82e2b668d0eef5cc0e1ab`, single parent `69ec419af13c0cb114928c249452ad8dd9729790`. The merged tree `de4c6b9e2bd8c85123ac5f847dbff06e85966cea` exactly matches qualified PR HEAD `3727d3b030acaf3cbe5c7ee6fdaab3bd9b4abe50`.

Publisher run 38885688934 passed. Actual push/main CI [38885689028](https://github.com/Coxf0rd/fwrouter/actions/runs/38885689028) failed. Backend wheel build and main smoke passed. The runtime-scale test failed the unchanged 45-second startup deadline at 45.0017 seconds; the qualified PR run completed at 44.2705 seconds. Aggregation errors cascade from the failed application receipt, not an independently established packaging/provenance defect. Packet Emergency Direct/re-entry passed. Native/profile cleanup reported owned resources removed.

Deployment is BLOCKED until a corrected main passes actual main CI. No production deployment/restart, quarantine, deletion or configuration change has occurred in this task.

## Evidence and investigation

Private evidence: `/root/fwrouter-recovery/post-pr16-main-recovery-20261016/` contains main logs/artifacts and a redacted cache inventory. The deadline diagnostics include global routing recovery 13.1231 s wall / 12.479 CPU, and profile publication 25.1613 s wall / 16.9913 CPU. These nested phases overlap with their children and must not be summed indiscriminately. The later post-stop tail drops earlier events; its omissions are not evidence that an earlier phase did not execute. Failure-time API CPU was 34.99 s and HWM about 721 MiB; no memory-limit failure has been established.

Two independent bounded investigations trace the routing and profile-publication paths. A source correction must remove confirmed work while preserving Core/native proof, credentials, fencing, fail-closed and recovery. Timeouts, memory limits and test assertions remain unchanged. Expected regressions must be authored before implementing the identified correction.

## Ruff cache inventory

`/opt/fwrouter-api/.ruff_cache` contains 11 regular files, total 24,042 bytes, root:root, mode 0644, under a root:root 0755 directory. They include Ruff's CACHEDIR.TAG, `.gitignore`, and version 0.17.0 cache entries. No matching open fd or cwd was observed in the bounded `/proc` inspection. No tracked backend Ruff-cache file exists; six files match current source-worktree counterparts. The installer overlays files and does not remove these extras. Exact creating command/process is UNKNOWN; timestamps and matching counterparts alone do not prove it.

The strict helper correctly rejects non-excluded installed extras before stopping services. This is not evidence of a dirty Git tree or a need to weaken admission. A reversible private quarantine, with manifest/hash verification, is authorized once rollout is eligible. It has NOT RUN; no unrelated cache/worktree or host file is to be removed.

## Remaining gates

Confirmed root cause and isolated reproduction; bounded source fix; targeted tests; independent QA; full required PR CI; user Squash and Merge; successful actual main CI; protected baseline/backups and cache quarantine; limited backend/docs rollout; bounded Live Acceptance. User-controlled merge is a mandatory stop gate. External subscription availability, full external E2E and complete host/reboot acceptance remain separate.

## Matched PR/main comparison

The fixture config is byte/metadata identical (20,722,965 bytes; 199,257 top-level and 199,257 transparent rules). PR startup was 44.2705 s / 34.35 process CPU s; main failed at 45.0017 s / 34.99 process CPU s. Global apply was 12.9571 wall / 12.3247 CPU s in PR and 13.1198 / 12.4757 in main. Profile recovery was 24.5136 / 16.5643 versus 25.1613 / 16.9913. This supports a pre-existing high-cost path crossing a strict boundary under small run-to-run variation; it does not establish a new source regression, an OOM or a separate main-event behavior bug. Removing confirmed unnecessary work is required to restore margin; rerunning unchanged code would not establish a fix.

## Rejected shortcut

The profile pipeline intentionally refuses native-validation reuse when its immutable Docker validator identity is unknown. The hosted harness validates a pinned binary through RPC but does not expose a Docker image identity to that gate. This is not permission to fabricate a Docker ID or skip unknown-validator proof. A generic transport-identity refactor is deferred. The measured 1.7–1.8 s local-validation call does not isolate RPC cost, so no saving from this candidate is claimed. The current correction instead investigates an already-invalid scalar metadata precheck doing unnecessary full YAML load/build before returning failure.

## Authorized reversible cache quarantine

After the initial read-only checkpoint, the user-authorized cache isolation was completed independently of deployment eligibility. Only the 11 identified files were archived and atomically moved to a root-private quarantine; no deletion or broad cleanup was used. The archive payload hashes and quarantined file hashes match the inventory. Recovery evidence: `RUFF_CACHE_QUARANTINE.json`, protected `ruff-cache-before-quarantine.tar`, `quarantine/ruff-cache-original/`. No source file, service, runtime config or network state was changed.

At 2026-10-16 10:43 UTC, the installed backend exactly matched all 443 expected files from deployed `69ec419`, extras zero. The helper's read-only layout preflight passed. Stop/resume was NOT RUN; this is not full deployment readiness, which remains BLOCKED by main CI. Historical initial “not quarantined” observations above describe the earlier checkpoint, not current cache state.

## Confirmed correction and matched measurements

`mihomo_runtime_satisfies_routing()` previously scanned three required scalar metadata values, then parsed the full YAML and rebuilt/guarded the large rules policy even when those scalar values already required a failed result. The exact synthetic fixture reproduced this path with empty metadata and no native health call. The correction returns the same failed reason before that unnecessary work. Uncomputed UDP diagnostic fields are explicitly `None`; when scalar metadata matches, full local policy comparison and native health proof remain unchanged. No cache or durable/native writer was introduced.

Unprofiled baseline wall/CPU samples were approximately 1.7921, 2.5926, 2.2588 seconds; median 2.2588. Matched after samples were 0.0807, 0.0738, 0.0762, 0.0725, 0.0727; median 0.0738. Reduction: about 2.185 seconds per known-invalid invocation (~96.7%). This is synthetic function cost, not hosted startup or production saving. Separate cProfile identified YAML construction and mandatory-rule reconstruction as the removed work; profiled wall time is not used to estimate the saving. The after profile is retained under `/root/fwrouter-recovery/startup-apply-profiler-20261016/after-profile.json`; raw initial profile output was observed but not saved, so that limitation remains explicit.

Tests for each missing/wrong scalar and the matching full-proof path were authored before implementation. The first RED execution failed to start pytest and is NOT behavioral RED evidence. Grouped isolated verification subsequently passed 213 tests across Mihomo runtime, bootstrap, routing drift and provider recovery. Exact historical node IDs remain unchanged. Independent QA and full hosted CI are separate pending gates at this checkpoint. The 45-second hard limit, memory limits and assertions were not increased or weakened.

## Independent source review

QA PASSED at service SHA-256 `32b7be515ef094c8e21fa0b0c8d06213bb46c519cdbb1ca159f45829e9c4cd57` and test SHA-256 `3904304201c45876b8a1bb353331ac26c66cdb32d27e3e83bb536765d07b141b`. It independently ran 60 Mihomo tests and 123 routing-drift/provider-recovery tests in an isolated network namespace/private state, and checked callers, atomic writes, native UDP proof and fencing. No material finding. The root final review, manifest validation (176 test files), changed-file F821, whitespace and installer-surface checks passed. Full required PR CI and post-user-merge main CI remain distinct gates.

Protected host hashes were independently rechecked after cache quarantine: 27/27 match the prior baseline, including the readiness guard. Backend source remains exactly deployed `69ec419` (443 files); only the identified tool cache was moved. No service restart, runtime installation or native/network state change was performed.
