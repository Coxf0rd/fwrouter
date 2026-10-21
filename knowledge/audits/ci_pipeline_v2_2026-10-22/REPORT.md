# CI Pipeline V2

## Scope and qualification boundary

Infrastructure only, based on main `059f49bfe130529607a5f49153d592cf487754dc`. No deployment, product code, provider operation, branch protection change, automatic merge or CD. One feature branch and one review PR; only the user merges. PR31 is already merged at that main commit; older roadmap checkpoints preserve their original recording-time status.

## Execution architecture

Eight GitHub-hosted Ubuntu 24.04 jobs: immutable plan/preflight, build, unit, ordinary hosted/browser, provider hosted/browser, packet, native, and final required result. Unit starts immediately after plan; build runs independently alongside it. Four isolated runtime jobs consume plan and the small source-bound wheel artifact after build. QCP and Docker Xray share one native job, with disjoint child runtime ownership; provider and ordinary application cohorts use separate runners. No shared mutable container, volume, process or state directory crosses runners.

The stable required check remains `Affected L0-L5 with hosted profiles`. Its `always()` job observes all seven producer results. Required failure, cancellation, skip or missing result fails; an unselected profile must be skipped. Missing plan/build evidence fails. Successful producer artifacts are bound to exact source SHA, canonical plan and manifest digests/version, GitHub run ID and attempt, role and file hashes. Duplicate, extra, missing, changed or symlinked files are rejected before consuming receipts. The original strict `aggregate_hosted.py` validates exact test coverage, profiles, node IDs, runtime hashes, native/isolation/cleanup proof, historical regressions and mandatory main smoke. Existing safe-recovery source/staging qualification follows only an eligible aggregate.

Preflight retains event-fixed base SHA, catalog/isolation/static validation, affected PR selection and mandatory main/full-acceptance selection. Main smoke is retained without duplicating its functional receipt. Push-fast and nightly L6 contracts are unchanged.

## Build and dependency decision

Backend compileall, wheel build and exact source/payload verification run once. Static JavaScript syntax checks run in build; existing Node regressions remain in the unit gate. Build dependencies have a separate SHA256-locked requirements file. Every consumer revalidates artifact identity and wheel payload against its exact checkout before executing a runtime profile.

V1 measurements show verified runtime provisioning takes about 5.3 seconds, while the normalized Chromium archive is 621,629,440 bytes (expanded 621,257,716 bytes). Shipping it to four runners would add substantial artifact transfer and storage for a small preparation stage. V2 therefore retains original pinned HTTPS/size/SHA256/version/provenance provisioning on each isolated runtime runner. The existing source-free digest-pinned GHCR dependency image is reused; V2 does not save/load the approximately 2.63 GB Docker image through artifacts. No mutable/latest binary or image is admitted. Only unit and native Xray install the full host test lock. Preflight installs hash-locked Ruff only; build installs its own build lock. Browser/provider/packet orchestration and aggregation use stdlib host helpers and the existing pinned container dependencies. Python requirements remain hash locked. Native receipts prove actual qualification rather than treating configuration parsing as connectivity proof.

## Baseline and measurement method

V1 reference: [run39658023287](https://github.com/Coxf0rd/fwrouter/actions/runs/39658023287), source `be4ec2aed9d66fabcd80029da9ddfde70116c37e`, successful full mandatory PR31 selection. Single required job: 19:01:54–19:23:13 UTC, **21m19s**. Routine stage393s; serial hosted/native stage852s; setup/preflight approximately24s; provision approximately6s. Wrapper measurements: ordinary334.96s, provider243.47s, packet231.41s, Docker Xray3.11s. First dependency image pull26.97s. Runner capacity4 vCPU/about15 GiB RAM. These measured historical values are not V2 speed claims.

Record actual V2 job intervals, workflow wall time including queueing, critical dependency path, total runner-minutes, dependency preparation and artifact step intervals using GitHub job API. Compare selected files/node coverage explicitly because V2 adds infrastructure regressions. Per-command `/usr/bin/time -v` records CPU and maximum process RSS; snapshots record runner CPU/memory/disk capacity and point-in-time usage. These do not measure peak aggregate Docker/cgroup memory or storage-controller writes. Artifact size is recorded from GitHub API. No acceleration or cost saving is claimed before real runs.

## Qualified V2 reference and comparison

[Run39665954573 attempt1](https://github.com/Coxf0rd/fwrouter/actions/runs/39665954573/attempts/1), exact source `5a44a5923bc5c0c577c086cebcaf101c771eff31`, passed all eight jobs. Strict aggregate: eligible=true,2121 registered node IDs,178 selected files, no blocked reasons or missing levels; three hosted partitions,42 QCP cases and one Docker Xray case passed with cleanup. Safe-recovery qualification and isolated staging passed. Unit's non-promoting deferred report is expected; only the final aggregate admits complete evidence.

V1 reference and V2 share the exact product source tree and the same178 selected files/2121 aggregate node IDs. V2 adds internal unittest cases within the existing gate suites; those suites retain their registered identities. These are single qualified observations on4-vCPU/about15-GiB hosted runners, not a statistically matched repeated benchmark. CPU models and global peak Docker memory were not recorded. Runner/network variance remains a limitation.

| Measurement | V1 | Qualified V2 reference |
| --- | ---: | ---: |
| Workflow wall, created through final update |21m23s|7m57s|
| Required job / sum of eight job durations |21m19s|23.93 runner-minutes|
| Plan/preflight job |part of single job|40s|
| Build job |PR wheel build not required|32s|
| Unit job |routine command393s|292s (command260.142s)|
| Ordinary hosted/browser job |wrapper334.96s|366s (wrapper324.331s)|
| Provider job |wrapper243.47s|316s (wrapper285.379s)|
| Packet job |wrapper231.41s|260s (wrapper221.228s)|
| Native job |serial QCP/Xray|102s (QCP60.588s; Xray2.672s)|
| Aggregate/recovery job |part of single job|28s|

Observed workflow wall is62.8% lower (2.69x shorter), while summed runner time is12.3% higher. V2 critical path is plan → build → ordinary hosted/browser → aggregate. Unit is independent and finishes before ordinary hosted. Job durations include setup/dependencies/artifact actions; V1 wrapper values are explicitly different boundaries and must not be interpreted as per-job speedups. Dependency setup is intentionally traded for isolated parallel runners. Runner-minutes here sum actual job durations; they are not invoice/billing-rounded minutes.

Build helper timings: dependency install0.665s, compileall0.615s, static JS0.613s, wheel0.565s. Per-runner native/browser provisioning: ordinary5.522s, provider7.049s, packet10.996s, native10.443s. Upload/download/retention action durations total approximately20 runner-seconds (one-second API resolution; overlapping, not20 seconds added to the critical path). Eight compressed artifacts total2,822,718 bytes; wheel artifact897,547 bytes; recovery/result artifact1,641,895 bytes. No Chromium bundle is transferred through Actions artifacts.

GNU time's largest recorded process/child RSS is640,552 KiB in unit (about625.5 MiB), CPU user190.38s/system35.20s. This excludes Docker daemon/container peaks; profile parent measurements must not be represented as total container cost. Runner snapshots record CPU count, memory availability and disk usage, not a continuous peak. Existing acceptance image/cgroup measurements remain in the profile receipts.

## Required-result negative qualification

- Initial run39664620080 failed one new noisy-log test because its expected output omitted `print`'s final newline. The implementation's live4-MiB disk limit and child exit code passed. The assertion was corrected and independently reviewed. Final required check failed explicitly with `unit: required upstream result is failure` despite all other producer jobs passing. A deleted build artifact in this initial run is not claimed as an independent missing-artifact proof because the upstream failure check ran first.
- After the successful reference run, only its final job was rerun using the GitHub job-rerun API. [Attempt2](https://github.com/Coxf0rd/fwrouter/actions/runs/39665954573/attempts/2) retained successful upstream statuses but had no current-attempt plan/producer artifacts. The final check failed, required-result eligible=false, with missing immutable plan evidence. Attempt1 artifacts were not accepted. A partial rerun is deliberately insufficient; rerun the whole workflow to obtain coherent same-attempt evidence.
- Contract tests cover every required producer's failure/cancelled/skipped/missing status, optional-profile skip versus unexpected execution, wrong SHA/plan/manifest/run/attempt, tampering/missing/extra/symlinked files, malformed upstream JSON and absent preflight. Existing strict aggregate negative tests preserve exact node coverage, failed/skipped/duplicated receipts, runtime pins and native proof.
- The required context is unchanged and remains GitHub Actions application15368. No ruleset/protection setting, assertion, profile coverage, timeout or resource limit was weakened. No `continue-on-error` exists.

## Final dependency optimization and review boundary

The reference above still installed the full host CI lock in every job. The final bounded optimization removes those redundant installations where orchestration is stdlib. Unit and native retain the full lock; native's Xray readback executes host pytest, so that dependency is required. Preflight retains only pinned Ruff. QA independently exercised helper imports with `python -S`, source/catalog/isolation checks and changed-source lint without third-party site packages, and traced safe-recovery through its isolated installer copy stage. Build dependencies remain hash locked and verified.

Source and grouped fixes passed independent review; targeted checks passed with one local noisy-child omission because `/usr/bin/time` is absent locally. That exact regression ran successfully on GitHub in the qualified reference. Final source adds one dependency-ownership regression:91 gate/175 acceptance internal unittest cases versus83/170 in V1. A final-review regression also preserves legitimate documentation-only L0 plans with zero selected test suites; the always-run build still verifies their source-bound wheel without forcing unrelated profiles. The same registered suites and full qualification remain required.

[PR32](https://github.com/Coxf0rd/fwrouter/pull/32) is the single integration point. Its latest required check is authoritative for the final optimized HEAD; final measured timings and run link are attached to the PR and retained Actions artifacts. Review readiness requires that check to be SUCCESS after the deliberate negative probe. The user alone may Squash and Merge. Main-event execution is a post-user-merge gate; its mandatory selection/smoke contracts are covered here, but no real main event was created. Production/deployment/live acceptance are outside this task and were not performed.
