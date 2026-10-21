# CI Pipeline V2

## Scope and qualification boundary

Infrastructure only, based on main `059f49bfe130529607a5f49153d592cf487754dc`. No deployment, product code, provider operation, branch protection change, automatic merge or CD. One feature branch and one review PR; only the user merges. PR31 is already merged at that main commit; older roadmap checkpoints preserve their original recording-time status.

## Execution architecture

Eight GitHub-hosted Ubuntu 24.04 jobs: immutable plan/preflight, build, unit, ordinary hosted/browser, provider hosted/browser, packet, native, and final required result. Unit starts immediately after plan; build runs independently alongside it. Four isolated runtime jobs consume plan and the small source-bound wheel artifact after build. QCP and Docker Xray share one native job, with disjoint child runtime ownership; provider and ordinary application cohorts use separate runners. No shared mutable container, volume, process or state directory crosses runners.

The stable required check remains `Affected L0-L5 with hosted profiles`. Its `always()` job observes all seven producer results. Required failure, cancellation, skip or missing result fails; an unselected profile must be skipped. Missing plan/build evidence fails. Successful producer artifacts are bound to exact source SHA, canonical plan and manifest digests/version, GitHub run ID and attempt, role and file hashes. Duplicate, extra, missing, changed or symlinked files are rejected before consuming receipts. The original strict `aggregate_hosted.py` validates exact test coverage, profiles, node IDs, runtime hashes, native/isolation/cleanup proof, historical regressions and mandatory main smoke. Existing safe-recovery source/staging qualification follows only an eligible aggregate.

Preflight retains event-fixed base SHA, catalog/isolation/static validation, affected PR selection and mandatory main/full-acceptance selection. Main smoke is retained without duplicating its functional receipt. Push-fast and nightly L6 contracts are unchanged.

## Build and dependency decision

Backend compileall, wheel build and exact source/payload verification run once. Static JavaScript syntax checks run in build; existing Node regressions remain in the unit gate. Build dependencies have a separate SHA256-locked requirements file. Every consumer revalidates artifact identity and wheel payload against its exact checkout before executing a runtime profile.

V1 measurements show verified runtime provisioning takes about 5.3 seconds, while the normalized Chromium archive is 621,629,440 bytes (expanded 621,257,716 bytes). Shipping it to four runners would add substantial artifact transfer and storage for a small preparation stage. V2 therefore retains original pinned HTTPS/size/SHA256/version/provenance provisioning on each isolated runtime runner. The existing source-free digest-pinned GHCR dependency image is reused; V2 does not save/load the approximately 2.63 GB Docker image through artifacts. No mutable/latest binary or image is admitted. Python requirements remain hash locked. Native receipts prove actual qualification rather than treating configuration parsing as connectivity proof.

## Baseline and measurement method

V1 reference: [run39658023287](https://github.com/Coxf0rd/fwrouter/actions/runs/39658023287), source `be4ec2aed9d66fabcd80029da9ddfde70116c37e`, successful full mandatory PR31 selection. Single required job: 19:01:54–19:23:13 UTC, **21m19s**. Routine stage393s; serial hosted/native stage852s; setup/preflight approximately24s; provision approximately6s. Wrapper measurements: ordinary334.96s, provider243.47s, packet231.41s, Docker Xray3.11s. First dependency image pull26.97s. Runner capacity4 vCPU/about15 GiB RAM. These measured historical values are not V2 speed claims.

Record actual V2 job intervals, workflow wall time including queueing, critical dependency path, total runner-minutes, dependency preparation and artifact step intervals using GitHub job API. Compare selected files/node coverage explicitly because V2 adds infrastructure regressions. Per-command `/usr/bin/time -v` records CPU and maximum process RSS; snapshots record runner CPU/memory/disk capacity and point-in-time usage. These do not measure peak aggregate Docker/cgroup memory or storage-controller writes. Artifact size is recorded from GitHub API. No acceleration or cost saving is claimed before real runs.

## Current gates

Source implemented; targeted regression, independent review and hosted qualification in progress. Required-result negative cases, final successful hosted run, performance comparison and PR readiness remain OPEN. Production and live acceptance are outside this task.
